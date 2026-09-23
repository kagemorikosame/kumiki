"""絵を止めたクリップ（``Clip.hold_at`` Issue #115）

YMM4 は素材より長い動画アイテムで最後の絵を、再生速度 0 で素材の頭の絵を出し続ける
それを写すための持ち方で、壊れ方は 3 通りある

- 編集の命令で止める位置がずれる 分割した後半が動き出したり、止める絵が変わったりする
- 保存し直すと消える 開き直しただけで止めた絵が動き出す
- 古いファイルが開けなくなる
"""

from __future__ import annotations

from fractions import Fraction

import pytest

from sashimono.core.commands import AddClip, RippleCut, SetClipProperty, SplitClip, TrimClip
from sashimono.core.io import FORMAT_VERSION, project_from_dict, project_to_dict
from sashimono.core.model import Clip, MediaItem, Project
from sashimono.core.timebase import FrameRate

RATE = FrameRate(30)


def _placed(project: Project, clip: Clip) -> Project:
    return AddClip(project.timeline.tracks[0].id, clip).apply(project)


def _clips(project: Project) -> tuple[Clip, ...]:
    return project.timeline.tracks[0].clips


def _times(clip: Clip) -> list[Fraction]:
    """クリップの頭から終わりまで、各フレームで絵を取る素材の時刻"""
    return [clip.picture_time(frame, RATE) for frame in range(clip.duration)]


@pytest.fixture
def held(project: Project, video_media: MediaItem) -> Project:
    """10 秒の素材の 1 秒目から読み、素材の 5 秒の所で止めるクリップ（300 フレーム）"""
    clip = Clip(
        timeline_start=0,
        duration=300,
        media_id=video_media.id,
        source_in=Fraction(1),
        hold_at=Fraction(5),
    )
    return _placed(project, clip)


class TestPictureTime:
    def test_the_picture_moves_until_the_hold(self) -> None:
        clip = Clip(timeline_start=0, duration=300, source_in=Fraction(1), hold_at=Fraction(5))
        # 止める前は素のクリップと同じく進む 止める所を早めに効かせると、動くはずの所が止まる
        assert clip.picture_time(30, RATE) == Fraction(2)
        assert clip.picture_time(120, RATE) == Fraction(5)

    def test_the_picture_never_reads_past_the_hold(self) -> None:
        clip = Clip(timeline_start=0, duration=300, source_in=Fraction(1), hold_at=Fraction(5))
        assert clip.picture_time(121, RATE) == Fraction(5)
        assert clip.picture_time(299, RATE) == Fraction(5)

    def test_a_hold_at_the_start_freezes_every_frame(self) -> None:
        # YMM4 の再生速度 0 は素材の頭の絵のまま 1 フレームでも進むと別の絵になる
        clip = Clip(timeline_start=0, duration=180, source_in=Fraction(2), hold_at=Fraction(2))
        assert set(_times(clip)) == {Fraction(2)}

    def test_a_clip_without_a_hold_keeps_reading_the_source(self) -> None:
        # 既定で止めると、素材の終わりまで使うだけのクリップが動かなくなる
        clip = Clip(timeline_start=0, duration=300, speed=Fraction(2))
        assert clip.picture_time(299, RATE) == Fraction(299, 15)

    def test_a_negative_hold_is_refused(self) -> None:
        # 素材の頭より前は無い 通すと、どの絵を出すのかが決まらない
        with pytest.raises(ValueError, match="止める時刻"):
            Clip(timeline_start=0, duration=10, hold_at=Fraction(-1))


class TestEditing:
    def test_the_later_half_of_a_split_stays_frozen(self, held: Project) -> None:
        """止める位置より後で割った後半は、ずっと止まった絵

        後半の ``source_in`` は止める位置より後になる 止める位置をクリップの中の経過で
        持っていると、後半は 0 から数え直して動き出す
        """
        (clip,) = _clips(held)
        _, right = _clips(SplitClip(clip.id, 200).apply(held))
        assert right.source_in > Fraction(5)
        assert set(_times(right)) == {Fraction(5)}

    def test_a_split_before_the_hold_keeps_the_same_pictures(self, held: Project) -> None:
        # 割っても、各フレームに出る絵は割る前と同じ 止める所が前半の長さの分ずれると、
        # 割った所から後の絵が早く止まる
        (clip,) = _clips(held)
        left, right = _clips(SplitClip(clip.id, 60).apply(held))
        assert _times(left) + _times(right) == _times(clip)

    def test_trimming_the_head_past_the_hold_freezes_the_rest(self, held: Project) -> None:
        (clip,) = _clips(held)
        (trimmed,) = _clips(TrimClip(clip.id, head_delta=150).apply(held))
        assert set(_times(trimmed)) == {Fraction(5)}

    def test_trimming_the_head_keeps_the_pictures_in_place(self, held: Project) -> None:
        # 頭を削ったぶん、残った所は同じ絵のまま 止める所がずれると、削っただけで止まる所が動く
        (clip,) = _clips(held)
        (trimmed,) = _clips(TrimClip(clip.id, head_delta=30).apply(held))
        assert _times(trimmed) == _times(clip)[30:]

    def test_a_faster_clip_still_stops_on_the_same_picture(self, held: Project) -> None:
        # 速さを変えても止める絵は同じ（素材の中の時刻で持つので） 止める所へ早く着くだけ
        (clip,) = _clips(held)
        (fast,) = _clips(SetClipProperty(clip.id, "speed", Fraction(2)).apply(held))
        assert fast.picture_time(60, RATE) == Fraction(5)
        assert fast.picture_time(299, RATE) == Fraction(5)

    def test_a_ripple_cut_keeps_the_rest_frozen(self, held: Project) -> None:
        # 無音のカットで真ん中を抜いても、残った後ろは止まったまま
        _, right = _clips(RippleCut(((100, 160),)).apply(held))
        assert set(_times(right)) == {Fraction(5)}

    def test_the_hold_can_be_released(self, held: Project) -> None:
        # インスペクタの「解除」 外せないと、読み込みで付いた止め方を直す道が無い
        (clip,) = _clips(held)
        (released,) = _clips(SetClipProperty(clip.id, "hold_at", None).apply(held))
        assert released.hold_at is None
        assert released.picture_time(299, RATE) == Fraction(1) + Fraction(299, 30)


class TestFile:
    def test_the_hold_survives_saving(self, held: Project) -> None:
        # 保存し直すと消えるなら、開き直しただけで止めた絵が動き出す
        (clip,) = _clips(project_from_dict(project_to_dict(held)))
        assert clip.hold_at == Fraction(5)

    def test_a_clip_without_a_hold_reopens_without_one(self, held: Project) -> None:
        (clip,) = _clips(held)
        loose = SetClipProperty(clip.id, "hold_at", None).apply(held)
        (reopened,) = _clips(project_from_dict(project_to_dict(loose)))
        assert reopened.hold_at is None

    def test_an_old_file_opens_without_holding(self, held: Project) -> None:
        """版 3 までのファイル（項目が無い）は止めないクリップとして開く

        項目が無いのを壊れたファイルとして断ると、今までのプロジェクトが全部開けなくなる
        """
        data = project_to_dict(held)
        data["version"] = 3
        timeline = data["timeline"]
        assert isinstance(timeline, dict)
        for track in timeline["tracks"]:
            for clip in track["clips"]:
                del clip["hold_at"]
        (clip,) = _clips(project_from_dict(data))
        assert clip.hold_at is None

    def test_the_format_version_says_the_hold_is_there(self, held: Project) -> None:
        # 版を上げないと、3 までの本体が項目を黙って捨て、保存し直すと止め方ごと消える
        assert FORMAT_VERSION >= 4
        assert project_to_dict(held)["version"] == FORMAT_VERSION

    def test_the_later_half_of_a_split_keeps_its_hold_after_saving(self, held: Project) -> None:
        # 割った後半も保存して開き直せる（別の ID でも止める位置を持つ）
        (clip,) = _clips(held)
        split = SplitClip(clip.id, 200).apply(held)
        reopened = project_from_dict(project_to_dict(split))
        assert [c.hold_at for c in _clips(reopened)] == [Fraction(5), Fraction(5)]
