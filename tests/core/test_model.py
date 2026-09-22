"""モデルの不変条件と補間の振る舞い"""

from __future__ import annotations

from fractions import Fraction
from itertools import pairwise

import pytest

from sashimono.core.model import (
    AnimatedValue,
    Clip,
    Effect,
    Interpolation,
    Keyframe,
    MediaItem,
    Project,
    ProjectSettings,
    Timeline,
    Track,
    TrackKind,
    Transcript,
    TranscriptSegment,
)
from sashimono.core.timebase import FrameRate
from tests.conftest import RATE_30, make_clip


class TestClip:
    def test_rejects_zero_duration(self, video_media: MediaItem) -> None:
        with pytest.raises(ValueError, match="1 フレーム以上"):
            Clip(timeline_start=0, duration=0, media_id=video_media.id)

    def test_rejects_non_positive_speed(self, video_media: MediaItem) -> None:
        with pytest.raises(ValueError, match="正でなければ"):
            Clip(timeline_start=0, duration=10, media_id=video_media.id, speed=Fraction(0))

    def test_source_out_accounts_for_speed(self, video_media: MediaItem) -> None:
        # 30 フレーム = 1 秒 2 倍速なら 2 秒分のソースを消費する
        clip = Clip(timeline_start=0, duration=30, media_id=video_media.id, speed=Fraction(2))
        assert clip.source_out(RATE_30) == Fraction(2)

    def test_contains_excludes_end(self, video_media: MediaItem) -> None:
        clip = make_clip(10, 20, video_media)
        assert clip.contains(10)
        assert clip.contains(29)
        assert not clip.contains(30)


class TestTrack:
    def test_rejects_overlapping_clips(self, video_media: MediaItem) -> None:
        with pytest.raises(ValueError, match="重なっている"):
            Track(
                kind=TrackKind.VIDEO,
                clips=(make_clip(0, 30, video_media), make_clip(29, 30, video_media)),
            )

    def test_allows_touching_clips(self, video_media: MediaItem) -> None:
        # 端が接するのは重なりではない カット直後の状態がこれ
        track = Track(
            kind=TrackKind.VIDEO,
            clips=(make_clip(0, 30, video_media), make_clip(30, 30, video_media)),
        )
        assert track.end_frame == 60

    def test_rejects_unsorted_clips(self, video_media: MediaItem) -> None:
        with pytest.raises(ValueError, match="開始位置順"):
            Track(
                kind=TrackKind.VIDEO,
                clips=(make_clip(60, 30, video_media), make_clip(0, 30, video_media)),
            )

    def test_with_clips_sorts(self, video_media: MediaItem) -> None:
        track = Track(kind=TrackKind.VIDEO).with_clips(
            (make_clip(60, 30, video_media), make_clip(0, 30, video_media))
        )
        assert [c.timeline_start for c in track.clips] == [0, 60]

    def test_clip_at(self, video_media: MediaItem) -> None:
        track = Track(kind=TrackKind.VIDEO, clips=(make_clip(10, 20, video_media),))
        assert track.clip_at(15) is not None
        assert track.clip_at(5) is None
        assert track.clip_at(30) is None


class TestTimeline:
    def test_duration_is_longest_track(self, video_media: MediaItem) -> None:
        timeline = Timeline(
            rate=RATE_30,
            tracks=(
                Track(kind=TrackKind.VIDEO, clips=(make_clip(0, 30, video_media),)),
                Track(kind=TrackKind.VIDEO, clips=(make_clip(0, 90, video_media),)),
            ),
        )
        assert timeline.duration == 90

    def test_empty_timeline_has_zero_duration(self) -> None:
        assert Timeline(rate=RATE_30).duration == 0

    def test_rejects_duplicate_track_ids(self) -> None:
        track = Track(kind=TrackKind.VIDEO)
        with pytest.raises(ValueError, match="重複"):
            Timeline(rate=RATE_30, tracks=(track, track))

    def test_rejects_inverted_work_area(self) -> None:
        with pytest.raises(ValueError, match="書き出し範囲"):
            Timeline(rate=RATE_30, work_area=(100, 100))


class TestProject:
    def test_rate_must_match_timeline(self) -> None:
        with pytest.raises(ValueError, match="一致しない"):
            Project(
                settings=ProjectSettings(frame_rate=FrameRate(30)),
                timeline=Timeline(rate=FrameRate(25)),
            )

    def test_create_uses_settings_rate(self) -> None:
        project = Project.create(ProjectSettings(frame_rate=FrameRate(25)))
        assert project.timeline.rate == FrameRate(25)

    def test_rejects_duplicate_media(self, video_media: MediaItem) -> None:
        with pytest.raises(ValueError, match="重複"):
            Project.create(media=(video_media, video_media))

    def test_require_media_raises_for_unknown(self, project: Project) -> None:
        from sashimono.core.model import MediaId

        with pytest.raises(KeyError):
            project.require_media(MediaId("存在しない"))


class TestAnimatedValue:
    def test_static_when_no_keyframes(self) -> None:
        value = AnimatedValue(static=42.0)
        assert not value.is_animated
        assert value.at(0) == 42.0
        assert value.at(10_000) == 42.0

    def test_linear_interpolation(self) -> None:
        value = AnimatedValue(
            keyframes=(Keyframe(frame=0, value=0.0), Keyframe(frame=10, value=100.0))
        )
        assert value.at(0) == 0.0
        assert value.at(5) == pytest.approx(50.0)
        assert value.at(10) == 100.0

    def test_clamps_outside_range(self) -> None:
        # 外挿するとトリムしただけで画面外へ飛んでいく 端で頭打ちにする
        value = AnimatedValue(
            keyframes=(Keyframe(frame=10, value=5.0), Keyframe(frame=20, value=15.0))
        )
        assert value.at(0) == 5.0
        assert value.at(1000) == 15.0

    def test_hold_keeps_left_value(self) -> None:
        value = AnimatedValue(
            keyframes=(
                Keyframe(frame=0, value=1.0, interpolation=Interpolation.HOLD),
                Keyframe(frame=10, value=9.0),
            )
        )
        assert value.at(5) == 1.0
        assert value.at(9) == 1.0
        assert value.at(10) == 9.0

    @pytest.mark.parametrize(
        "interpolation",
        [Interpolation.EASE_IN, Interpolation.EASE_OUT, Interpolation.EASE_IN_OUT],
    )
    def test_easing_stays_monotonic_between_endpoints(self, interpolation: Interpolation) -> None:
        value = AnimatedValue(
            keyframes=(
                Keyframe(frame=0, value=0.0, interpolation=interpolation),
                Keyframe(frame=100, value=1.0),
            )
        )
        samples = [value.at(f) for f in range(0, 101)]
        assert samples[0] == pytest.approx(0.0, abs=1e-6)
        assert samples[-1] == pytest.approx(1.0, abs=1e-6)
        assert all(b >= a - 1e-9 for a, b in pairwise(samples))

    def test_ease_in_starts_slower_than_linear(self) -> None:
        keyframes = (Keyframe(frame=0, value=0.0), Keyframe(frame=100, value=1.0))
        linear = AnimatedValue(keyframes=keyframes)
        eased = AnimatedValue(
            keyframes=(
                Keyframe(frame=0, value=0.0, interpolation=Interpolation.EASE_IN),
                Keyframe(frame=100, value=1.0),
            )
        )
        assert eased.at(20) < linear.at(20)

    def test_custom_bezier(self) -> None:
        value = AnimatedValue(
            keyframes=(
                Keyframe(
                    frame=0,
                    value=0.0,
                    interpolation=Interpolation.BEZIER,
                    control_points=(0.0, 0.0, 1.0, 1.0),
                ),
                Keyframe(frame=10, value=10.0),
            )
        )
        # (0,0,1,1) は直線と同じ曲線
        assert value.at(5) == pytest.approx(5.0, abs=1e-4)

    def test_a_named_curve_overshoots_like_back(self) -> None:
        """曲線の名前（``back``）を持つキーフレームは、その形で動く

        名前を落として 3 種の加減速へ丸めると、行き過ぎて戻る動きが消え、
        YMM4 の Back_InOut で回るローテンショントランジションが 30 度ずれた
        """
        value = AnimatedValue(
            keyframes=(
                Keyframe(frame=0, value=0.0, interpolation=Interpolation.EASE_IN_OUT, curve="back"),
                Keyframe(frame=100, value=180.0),
            )
        )
        # Back_InOut は前半で一度逆へ振れ、後半で行き過ぎる
        assert min(value.at(f) for f in range(0, 101)) < -5.0
        assert max(value.at(f) for f in range(0, 101)) > 185.0
        assert value.at(100) == 180.0

    def test_the_curve_takes_its_direction_from_the_interpolation(self) -> None:
        # Quart_In は 4 乗 向き（In）は補間方法が持つ 25% の所で 0.25^4
        value = AnimatedValue(
            keyframes=(
                Keyframe(frame=0, value=0.0, interpolation=Interpolation.EASE_IN, curve="quart"),
                Keyframe(frame=100, value=1.0),
            )
        )
        assert value.at(25) == pytest.approx(0.25**4)

    def test_a_curve_on_a_straight_point_is_rejected(self) -> None:
        # 直線や瞬間移動の点は曲線を読まない 持たせると効かない名前がファイルに残り、
        # 読んだ人が「Back で動くはず」と取り違える
        with pytest.raises(ValueError, match="イージング"):
            Keyframe(frame=0, value=0.0, interpolation=Interpolation.LINEAR, curve="back")

    def test_an_unknown_curve_is_rejected(self) -> None:
        # 知らない名前を通すと、描くときに直線へ落ちて誰も気付かない
        with pytest.raises(ValueError, match="曲線"):
            Keyframe(frame=0, value=0.0, interpolation=Interpolation.EASE_IN, curve="魔法")

    def test_bezier_requires_control_points(self) -> None:
        with pytest.raises(ValueError, match="control_points"):
            Keyframe(frame=0, value=0.0, interpolation=Interpolation.BEZIER)

    def test_rejects_unsorted_keyframes(self) -> None:
        with pytest.raises(ValueError, match="昇順"):
            AnimatedValue(keyframes=(Keyframe(frame=10, value=0.0), Keyframe(frame=0, value=1.0)))

    def test_rejects_duplicate_frames(self) -> None:
        with pytest.raises(ValueError, match="複数のキーフレーム"):
            AnimatedValue(keyframes=(Keyframe(frame=5, value=0.0), Keyframe(frame=5, value=1.0)))


class TestEffect:
    def test_with_param_does_not_mutate_original(self) -> None:
        original = Effect(kind="blur", params={"radius": AnimatedValue(4.0)})
        updated = original.with_param("radius", AnimatedValue(8.0))
        assert original.params["radius"] == AnimatedValue(4.0)
        assert updated.params["radius"] == AnimatedValue(8.0)
        assert updated.id == original.id


class TestTranscript:
    def test_rejects_inverted_segment(self) -> None:
        with pytest.raises(ValueError, match="終了が開始より前"):
            TranscriptSegment(start=Fraction(5), end=Fraction(1), text="x")

    def test_rejects_unsorted_segments(self) -> None:
        with pytest.raises(ValueError, match="昇順"):
            Transcript(
                segments=(
                    TranscriptSegment(start=Fraction(5), end=Fraction(6), text="b"),
                    TranscriptSegment(start=Fraction(1), end=Fraction(2), text="a"),
                )
            )

    def test_overlapping_excludes_touching(self, transcript: Transcript) -> None:
        # 1..3 のセグメントに対して 3..4 は接するだけ 重なり扱いしない
        assert list(transcript.overlapping(Fraction(3), Fraction(4))) == []
        assert len(list(transcript.overlapping(Fraction(2), Fraction(5)))) == 2

    def test_with_text_marks_edited(self, transcript: Transcript) -> None:
        segment = transcript.segments[0]
        updated = segment.with_text("こんにちは")
        assert updated.text == "こんにちは"
        assert updated.edited
        assert updated.id == segment.id
        assert not segment.edited

    def test_replace_segment(self, transcript: Transcript) -> None:
        updated = transcript.replace_segment(transcript.segments[1].with_text("差し替え"))
        assert updated.segments[1].text == "差し替え"
        assert updated.segments[0].text == transcript.segments[0].text

    def test_replace_unknown_segment_raises(self, transcript: Transcript) -> None:
        stranger = TranscriptSegment(start=Fraction(0), end=Fraction(1), text="x")
        with pytest.raises(KeyError):
            transcript.replace_segment(stranger)
