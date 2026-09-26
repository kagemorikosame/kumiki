"""タイムラインの磁石（吸着） クリップを動かす・端を伸び縮みさせる・置くときに近くへ吸い付く

吸い付く先は利用者の決定で 3 種類
- ほかのクリップの頭と終わり（どのトラックの物も 動かしている物は除く）
- 再生位置
- キーフレームのコマと、書き出し範囲の端

画面（ウィジェット）から切り離してある 窓を作らずに試験で押さえるため 吸い付く距離は
画面の画素で決め、フレームへ直すのは呼ぶ側の倍率で行う（拡大しても縮小しても、指で
感じる吸い付きの強さを変えない）
"""

from __future__ import annotations

import bisect
from collections.abc import Collection, Sequence

from sashimono.core.model import ClipId, Project
from sashimono.ui.timeline.keyframes import keyframe_frames

__all__ = ["DEFAULT_SNAP_DISTANCE", "nearest_snap", "snap_targets"]

#: 吸い付く距離の既定（画面の画素） 小さいと吸い付いたことに気付けず、大きいと
#: 1 コマずらしたいときに隣のクリップの端へ引き戻される
DEFAULT_SNAP_DISTANCE = 8


def snap_targets(project: Project, playhead: int, *, exclude: Collection[ClipId] = ()) -> list[int]:
    """吸い付く先のフレーム（小さい順 重なりは 1 つ）

    ``exclude`` は動かしているクリップ 自分の端やキーフレームへ吸い付くと、動かした量が
    0 に引き戻されて動かせない
    """
    frames = {playhead}
    timeline = project.timeline
    for track in timeline.tracks:
        for clip in track.clips:
            if clip.id in exclude:
                continue
            frames.add(clip.timeline_start)
            frames.add(clip.timeline_end)
            frames.update(clip.timeline_start + local for local in keyframe_frames(clip))
    if timeline.work_area is not None:
        frames.update(timeline.work_area)
    return sorted(frames)


def nearest_snap(
    edges: Sequence[int], targets: Sequence[int], reach: float
) -> tuple[int, int] | None:
    """``edges``（動かしている物の端）のどれかが ``reach`` フレーム以内に近づいた吸い付く先

    返すのは ``(ずらす量, 吸い付いた先)`` 一番近い物 無ければ ``None``
    ``targets`` は小さい順（:func:`snap_targets`）
    """
    best: tuple[int, int] | None = None
    for edge in edges:
        at = bisect.bisect_left(targets, edge)
        for index in (at - 1, at):
            if not 0 <= index < len(targets):
                continue
            shift = targets[index] - edge
            if abs(shift) <= reach and (best is None or abs(shift) < abs(best[0])):
                best = (shift, targets[index])
    return best
