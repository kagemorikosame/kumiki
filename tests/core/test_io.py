"""プロジェクトファイルの読み書き"""

from __future__ import annotations

import json
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import pytest

from kumiki.core.commands import SetTranscript
from kumiki.core.io import (
    FORMAT_NAME,
    FORMAT_VERSION,
    ProjectFileError,
    load_project,
    project_from_dict,
    project_to_dict,
    save_project,
)
from kumiki.core.model import (
    AnimatedValue,
    Clip,
    MediaItem,
    Project,
    ProjectSettings,
    Track,
    TrackKind,
    Transcript,
    TranscriptSegment,
)
from kumiki.core.timebase import FrameRate


class TestRoundTrip:
    def test_empty_project(self) -> None:
        original = Project.create()
        assert project_from_dict(project_to_dict(original)) == original

    def test_rich_project(self, rich_project: Project) -> None:
        assert project_from_dict(project_to_dict(rich_project)) == rich_project

    def test_survives_a_file(self, rich_project: Project, tmp_path: Path) -> None:
        path = tmp_path / "配信回_07.kmk"
        save_project(rich_project, path)
        assert load_project(path) == rich_project

    def test_repeated_save_load_is_stable(self, rich_project: Project, tmp_path: Path) -> None:
        # 分数を浮動小数で書き出していると、往復のたびに値が動く
        path = tmp_path / "p.kmk"
        current = rich_project
        for _ in range(5):
            save_project(current, path)
            current = load_project(path)
        assert current == rich_project

    def test_fractions_are_written_as_strings(self, tmp_path: Path) -> None:
        settings = ProjectSettings(frame_rate=FrameRate(30000, 1001))
        project = Project.create(settings)
        path = tmp_path / "ntsc.kmk"
        save_project(project, path)

        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["settings"]["frame_rate"] == "30000/1001"
        assert data["timeline"]["rate"] == "30000/1001"

    def test_japanese_is_not_escaped(self, rich_project: Project, tmp_path: Path) -> None:
        # 手で読める・手で直せることが JSON にした理由なので、ここは崩さない
        path = tmp_path / "p.kmk"
        save_project(rich_project, path)
        assert "配信回_07" in path.read_text(encoding="utf-8")


class TestFileHandling:
    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        path = tmp_path / "深い" / "階層" / "p.kmk"
        save_project(Project.create(), path)
        assert path.exists()

    def test_no_temporary_file_is_left_behind(self, tmp_path: Path) -> None:
        path = tmp_path / "p.kmk"
        save_project(Project.create(), path)
        assert [p.name for p in tmp_path.iterdir()] == ["p.kmk"]

    def test_overwrites_existing_file(self, tmp_path: Path) -> None:
        path = tmp_path / "p.kmk"
        save_project(Project.create(name="一回目"), path)
        save_project(Project.create(name="二回目"), path)
        assert load_project(path).name == "二回目"

    def test_untitled_project_takes_its_filename(self, tmp_path: Path) -> None:
        path = tmp_path / "夏の思い出.kmk"
        save_project(Project.create(), path)
        assert load_project(path).name == "夏の思い出"

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(ProjectFileError, match="開けない"):
            load_project(tmp_path / "無い.kmk")

    def test_not_json(self, tmp_path: Path) -> None:
        path = tmp_path / "p.kmk"
        path.write_text("これは JSON ではない", encoding="utf-8")
        with pytest.raises(ProjectFileError, match="JSON として読めない"):
            load_project(path)


class TestValidation:
    def test_rejects_foreign_format(self) -> None:
        with pytest.raises(ProjectFileError, match="プロジェクトファイルではない"):
            project_from_dict({"format": "aviutl", "version": 1})

    def test_rejects_newer_version(self) -> None:
        with pytest.raises(ProjectFileError, match="新しい形式"):
            project_from_dict({"format": FORMAT_NAME, "version": FORMAT_VERSION + 1})

    def test_rejects_rate_mismatch(self, rich_project: Project) -> None:
        data = project_to_dict(rich_project)
        data["timeline"]["rate"] = "25/1"
        with pytest.raises(ProjectFileError, match="食い違っている"):
            project_from_dict(data)

    def test_rejects_unknown_track_kind(self, rich_project: Project) -> None:
        data = project_to_dict(rich_project)
        data["timeline"]["tracks"][0]["kind"] = "subtitle"
        with pytest.raises(ProjectFileError, match="未知のトラック種別"):
            project_from_dict(data)

    def test_rejects_unknown_interpolation(self, rich_project: Project) -> None:
        data = project_to_dict(rich_project)
        clip = data["timeline"]["tracks"][0]["clips"][0]
        clip["effects"][0]["params"]["radius"]["keyframes"][0]["interpolation"] = "魔法"
        with pytest.raises(ProjectFileError, match="未知の補間方法"):
            project_from_dict(data)

    def test_rejects_a_keyframe_value_that_is_not_a_number(self, rich_project: Project) -> None:
        """キーフレームの値は**読み込みの時点で**数に限る

        ここを通すと、数でない値が AnimatedValue に入ったまま描画や音の計算へ
        流れ、math.isfinite の所で落ちる 水際で弾く
        """
        data = project_to_dict(rich_project)
        clip = data["timeline"]["tracks"][0]["clips"][0]
        clip["effects"][0]["params"]["radius"]["keyframes"][0]["value"] = "おおきめ"
        with pytest.raises(ProjectFileError, match="数値ではない"):
            project_from_dict(data)

    def test_rejects_a_keyframe_value_that_is_not_finite(self, rich_project: Project) -> None:
        # JSON の読み込みは NaN と Infinity を受けてしまう 通すと GL の値が壊れる
        data = project_to_dict(rich_project)
        clip = data["timeline"]["tracks"][0]["clips"][0]
        clip["effects"][0]["params"]["radius"]["keyframes"][0]["value"] = float("nan")
        with pytest.raises(ProjectFileError, match="有限の数ではない"):
            project_from_dict(data)

    def test_rejects_malformed_fraction(self, rich_project: Project) -> None:
        data = project_to_dict(rich_project)
        data["settings"]["frame_rate"] = "さんじゅう"
        with pytest.raises(ProjectFileError, match="分数として読めない"):
            project_from_dict(data)

    def test_rejects_wrong_type(self, rich_project: Project) -> None:
        data = project_to_dict(rich_project)
        data["settings"]["width"] = "1920"
        with pytest.raises(ProjectFileError, match="整数ではない"):
            project_from_dict(data)


class TestCompactness:
    def test_static_values_do_not_write_keyframes(self, tmp_path: Path) -> None:
        # プロジェクトファイルの大半は静的な値 ここが冗長だとファイルが膨れる
        project = Project.create()
        clip = Clip(timeline_start=0, duration=10, opacity=AnimatedValue(0.5))
        track = Track(kind=TrackKind.VIDEO, clips=(clip,))
        project = project.with_timeline(replace(project.timeline, tracks=(track,)))

        data = project_to_dict(project)
        assert data["timeline"]["tracks"][0]["clips"][0]["opacity"] == {"static": 0.5}


class TestTranscriptRoundTrip:
    def test_words_and_speaker_survive(self, rich_project: Project) -> None:
        restored = project_from_dict(project_to_dict(rich_project))
        media = next(m for m in restored.media if m.transcript is not None)
        assert media.transcript is not None
        first = media.transcript.segments[0]
        assert first.speaker == "話者A"
        assert [w.text for w in first.words] == ["今日", "は"]

    def test_edited_flag_survives(self, project: Project, video_media: MediaItem) -> None:
        transcript = Transcript(
            segments=(
                TranscriptSegment(start=Fraction(0), end=Fraction(1), text="編集済み", edited=True),
            )
        )
        source = SetTranscript(video_media.id, transcript).apply(project)
        restored = project_from_dict(project_to_dict(source))
        stored = restored.require_media(video_media.id).transcript
        assert stored is not None
        assert stored.segments[0].edited
