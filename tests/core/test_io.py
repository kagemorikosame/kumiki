"""プロジェクトファイルの読み書き"""

from __future__ import annotations

import json
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import pytest

from kumiki.core.commands import AddClip, AddMedia, AddTrack, SetTranscript
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
    Effect,
    Interpolation,
    Keyframe,
    Marker,
    MediaItem,
    Project,
    ProjectSettings,
    Track,
    TrackKind,
    Transcript,
    TranscriptSegment,
    Word,
)
from kumiki.core.timebase import FrameRate
from tests.conftest import make_clip


@pytest.fixture
def rich_project(
    project: Project, video_media: MediaItem, audio_media: MediaItem, transcript: Transcript
) -> Project:
    """ひととおりの要素が入ったプロジェクト 往復テストの対象"""
    result = AddMedia(audio_media).apply(project)
    result = SetTranscript(
        video_media.id,
        replace(
            transcript,
            segments=(
                replace(
                    transcript.segments[0],
                    words=(
                        Word(start=Fraction(1), end=Fraction(2), text="今日"),
                        Word(start=Fraction(2), end=Fraction(3), text="は"),
                    ),
                    speaker="話者A",
                ),
                *transcript.segments[1:],
            ),
        ),
    ).apply(result)

    audio_track = Track(kind=TrackKind.AUDIO, name="A1", volume_db=-3.5, pan=0.25)
    result = AddTrack(audio_track).apply(result)

    video_clip = replace(
        make_clip(0, 300, video_media),
        speed=Fraction(3, 2),
        effects=(
            Effect(
                kind="blur",
                params={
                    "radius": AnimatedValue(
                        keyframes=(
                            Keyframe(frame=0, value=0.0, interpolation=Interpolation.EASE_IN),
                            Keyframe(
                                frame=60,
                                value=24.0,
                                interpolation=Interpolation.BEZIER,
                                control_points=(0.1, 0.2, 0.3, 0.4),
                            ),
                            Keyframe(frame=120, value=4.0, interpolation=Interpolation.HOLD),
                        )
                    ),
                    "color": (1.0, 0.5, 0.25, 1.0),
                    "invert": True,
                    "quality": 3,
                    "label": "ぼかし",
                },
            ),
        ),
        opacity=AnimatedValue(
            keyframes=(Keyframe(frame=0, value=0.0), Keyframe(frame=30, value=1.0))
        ),
    )
    result = AddClip(result.timeline.tracks[0].id, video_clip).apply(result)
    result = AddClip(audio_track.id, make_clip(0, 900, audio_media)).apply(result)

    timeline = replace(
        result.timeline,
        markers=(Marker(frame=120, label="ここから本編", color="#ff0000"),),
        work_area=(30, 600),
    )
    return result.with_timeline(timeline).renamed("配信回_07")


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
