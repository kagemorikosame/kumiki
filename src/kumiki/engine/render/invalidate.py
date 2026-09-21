"""編集で、どのフレームの絵が変わったかを求める

先読みした絵を捨てる範囲を決めるために使う 編集のたびに全部捨てると、
先読みは「編集していない間だけ効く仕組み」になってしまう 1 本のクリップを
触ったときに、離れた場所の絵まで作り直すのは無駄

**足りない方へ倒さない** 変わったのに捨て損ねると、古い絵が画面に残る
判断が付かないものは :meth:`Invalidation.all` にする（作り直すだけで済む）
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace

from kumiki.core.model import (
    Clip,
    MediaId,
    Project,
    SceneId,
    Timeline,
    Track,
    TrackKind,
)

__all__ = ["Invalidation", "changed_spans"]


@dataclass(frozen=True, slots=True)
class Invalidation:
    """捨てるフレームの範囲 ``spans`` は半開区間 ``[start, end)`` の並び"""

    #: 全部捨てる 範囲を出せない種類の変更（解像度、トラックの重ね順）
    everything: bool = False
    spans: tuple[tuple[int, int], ...] = ()

    @classmethod
    def all(cls) -> Invalidation:
        return cls(everything=True)

    @classmethod
    def nothing(cls) -> Invalidation:
        return cls()

    @classmethod
    def over(cls, spans: Iterable[tuple[int, int]]) -> Invalidation:
        """範囲の並びから作る 重なりと隣り合わせはまとめる

        まとめないと、1 フレームずつ足した範囲が数千個たまり、
        1 枚捨てるかどうかを決めるのにその全部を見ることになる
        """
        ordered = sorted((start, end) for start, end in spans if end > start)
        merged: list[tuple[int, int]] = []
        for start, end in ordered:
            if merged and start <= merged[-1][1]:
                previous = merged[-1]
                merged[-1] = (previous[0], max(previous[1], end))
                continue
            merged.append((start, end))
        return cls(spans=tuple(merged))

    def __bool__(self) -> bool:
        """捨てるものがあるか"""
        return self.everything or bool(self.spans)

    def contains(self, frame: int) -> bool:
        if self.everything:
            return True
        return any(start <= frame < end for start, end in self.spans)


def changed_spans(before: Project, after: Project) -> Invalidation:
    """``before`` から ``after`` への編集で、絵が変わるフレームの範囲

    映像だけを見る 音量やパンを動かしても絵は変わらないので、そこで
    先読みを捨てると、音を合わせている間ずっと作り直し続けることになる
    """
    if before is after:
        return Invalidation.nothing()
    if before.settings.resolution != after.settings.resolution:
        return Invalidation.all()
    if before.rate != after.rate:
        # 同じフレーム番号が別の時刻を指すようになる 絵は全部変わる
        return Invalidation.all()

    media = _changed_media(before, after)
    scenes = _changed_scenes(before, after, media)
    return _timeline_spans(before.timeline, after.timeline, scenes, media)


def _timeline_spans(
    before: Timeline, after: Timeline, scenes: set[SceneId], media: set[MediaId]
) -> Invalidation:
    """メインのタイムラインで、絵が変わる範囲"""
    old_tracks = before.active_tracks(TrackKind.VIDEO)
    new_tracks = after.active_tracks(TrackKind.VIDEO)
    if [t.id for t in old_tracks] != [t.id for t in new_tracks]:
        # 重ね順が変わる・見えるトラックが増減する どちらも、そのトラックに
        # クリップが無いフレームの絵まで変わりうる（下の絵が透ける）
        return Invalidation.all()

    spans: list[tuple[int, int]] = []
    for old, new in zip(old_tracks, new_tracks, strict=True):
        if replace(old, clips=()) != replace(new, clips=()):
            # トラック全体のフィルタなどが変わった 掛かるのはクリップのある所だけ
            spans.extend(_span(clip) for clip in (*old.clips, *new.clips))
            continue
        spans.extend(_clip_spans(old, new))

    # 変更前と変更後の**両方**を見る 素材やシーンを消しながらクリップを動かす編集では、
    # 動かす前の場所が変更後のトラックには残っていない
    for track in (*old_tracks, *new_tracks):
        spans.extend(_span(clip) for clip in track.clips if _depends_on(clip, scenes, media))

    return Invalidation.over(spans)


def _clip_spans(before: Track, after: Track) -> list[tuple[int, int]]:
    """同じトラックの中で、変わったクリップの占める範囲（変更前と変更後の両方）

    動かしたクリップは、消えた場所と現れた場所の両方の絵が変わる
    """
    old = {clip.id: clip for clip in before.clips}
    new = {clip.id: clip for clip in after.clips}
    spans: list[tuple[int, int]] = []
    for clip_id in old.keys() | new.keys():
        first, second = old.get(clip_id), new.get(clip_id)
        if first == second:
            continue
        spans.extend(_span(clip) for clip in (first, second) if clip is not None)
    return spans


def _span(clip: Clip) -> tuple[int, int]:
    return clip.timeline_start, clip.timeline_end


def _depends_on(clip: Clip, scenes: set[SceneId], media: set[MediaId]) -> bool:
    """そのクリップが、中身の変わったシーンか素材を読んでいるか"""
    if clip.scene_id is not None and clip.scene_id in scenes:
        return True
    return clip.media_id is not None and clip.media_id in media


def _media_used(timeline: Timeline) -> set[MediaId]:
    """そのタイムラインが読んでいる素材"""
    return {
        clip.media_id
        for track in timeline.tracks
        for clip in track.clips
        if clip.media_id is not None
    }


def _changed_media(before: Project, after: Project) -> set[MediaId]:
    """中身の変わった素材 差し替えと読み込み直しで絵が変わる"""
    old = {item.id: item for item in before.media}
    new = {item.id: item for item in after.media}
    return {key for key in old.keys() | new.keys() if old.get(key) != new.get(key)}


def _changed_scenes(before: Project, after: Project, media: set[MediaId]) -> set[SceneId]:
    """中身の変わったシーン 入れ子で参照しているシーンも変わったとみなす

    シーンの中の 1 フレームが、外では何フレーム目に出るかは、置いたクリップの
    速度と開始位置で決まる そこまで追わず、**そのシーンを置いたクリップ全体**を
    捨てる シーンは繰り返し使う部品で、編集の頻度が低い

    **素材の差し替えも中身の変化として数える** シーンの中のクリップは素材を
    ID で指しているので、差し替えてもタイムラインは同じまま それを見落とすと、
    シーンの中で使っている素材を差し替えたときに、外へ置いた所だけ古い絵が残る
    """
    old = {scene.id: scene.timeline for scene in before.scenes}
    new = {scene.id: scene.timeline for scene in after.scenes}
    changed = {key for key in old.keys() | new.keys() if old.get(key) != new.get(key)}
    changed |= {
        scene_id
        for timelines in (old, new)
        for scene_id, timeline in timelines.items()
        if _media_used(timeline) & media
    }

    # 変わったシーンを置いているシーンも、外から見れば変わっている
    # 数えきるまで繰り返す（シーンの入れ子は循環しないことがモデル側で保証されている）
    while True:
        spread = {
            scene_id
            for timelines in (old, new)
            for scene_id, timeline in timelines.items()
            if scene_id not in changed and timeline.scene_references() & changed
        }
        if not spread:
            return changed
        changed |= spread
