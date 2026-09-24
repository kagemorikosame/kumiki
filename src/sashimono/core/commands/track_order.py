"""トラックの並べ替え

どのトラックどうしを入れ替えてよいかは :func:`reorder_group` の 1 か所で決める
いまは同じ種類（映像どうし・音声どうし）の中だけ 映像トラックの並びは重ね順
（後ろにあるほど手前）で、音声トラックの並びは足し合わせるだけなので聞こえ方は変わらない
種類をまたいで動かしても、画面は種類ごとにまとめて並べるので（映像が上、音声が下）
置いた所に出ず、映像のクリップを音声の欄へ運ぶことにもならない

1 本のレイヤーに映像も音声も置ける形（YMM4 のレイヤー）へ広げるときは、
:func:`reorder_group` を「全部のトラック」に差し替えれば、コマンドと画面の両方が付いてくる
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from sashimono.core.commands.base import Command
from sashimono.core.model import Project, Timeline, Track, TrackId

__all__ = ["MoveTrack", "reorder_group"]


def reorder_group(timeline: Timeline, track: Track) -> tuple[Track, ...]:
    """``track`` と並びを入れ替えられるトラック（自分を含む） 並びは :attr:`Timeline.tracks` のまま

    並べ替えの規則はここだけに置く 画面の側（どこへ落とせるか）もコマンドの側
    （何番目へ動かすか）も、これで仲間を数える
    """
    return tuple(t for t in timeline.tracks if t.kind is track.kind)


@dataclass(frozen=True, slots=True)
class MoveTrack(Command):
    """トラックを、仲間（:func:`reorder_group`）の中の ``index`` 番目へ動かす

    番号は :attr:`Timeline.tracks` の並びで数える（画面の上からではない） 映像は後ろほど
    手前に重なるので、画面の上へ動かすほど番号が大きい 仲間でないトラックの位置は変えない
    （映像と音声が交互に並んだファイルでも、音声の側の並びがずれない）

    ロックしたトラックは動かさない 並びを変えると重ね順が変わり、ロックで守っている
    はずの絵が変わる
    """

    track_id: TrackId
    index: int

    @property
    def label(self) -> str:
        return "トラックの順序を変更"

    def apply(self, project: Project) -> Project:
        timeline = project.timeline
        track = timeline.find_track(self.track_id)
        if track is None:
            raise KeyError(f"トラックが見つからない: {self.track_id}")
        if track.locked:
            raise ValueError(f"ロックしたトラックは動かせない: {track.name or track.kind.value}")
        group = [t for t in reorder_group(timeline, track) if t.id != track.id]
        if not 0 <= self.index <= len(group):
            raise ValueError(f"動かす先の番号が範囲の外: {self.index}（0〜{len(group)}）")
        others = [t for t in timeline.tracks if t.id != track.id]
        if self.index < len(group):
            # 仲間の index 番目の直前へ入れる 仲間でないトラックはその場に残る
            at = others.index(group[self.index])
        else:
            at = others.index(group[-1]) + 1 if group else len(others)
        others.insert(at, track)
        moved = tuple(others)
        if moved == timeline.tracks:
            return project
        return project.with_timeline(replace(timeline, tracks=moved))
