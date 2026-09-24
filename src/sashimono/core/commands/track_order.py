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
        # 仲間が占めていた席だけを、新しい並びで埋め直す 抜いて差し込むと、仲間でない
        # トラックの番号が 1 つずれ、保存したファイルの並びまで変わる
        members = {t.id for t in reorder_group(timeline, track)}
        seats = [i for i, t in enumerate(timeline.tracks) if t.id in members]
        group.insert(self.index, track)
        ordered = list(timeline.tracks)
        for seat, replacement in zip(seats, group, strict=True):
            ordered[seat] = replacement
        moved = tuple(ordered)
        if moved == timeline.tracks:
            return project
        return project.with_timeline(replace(timeline, tracks=moved))
