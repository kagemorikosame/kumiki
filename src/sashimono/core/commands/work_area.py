"""書き出し範囲（:attr:`Timeline.work_area`）を決めるコマンド

範囲はタイムラインごとに持つ シーンを開いているときは :class:`InScene` で包まれて、
そのシーンの範囲が変わる 書き出しが使うのはメインの範囲だけ（書き出すのはメイン）
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from sashimono.core.commands.base import Command
from sashimono.core.model import Project, Timeline

__all__ = ["SetWorkArea", "export_range"]


@dataclass(frozen=True, slots=True)
class SetWorkArea(Command):
    """書き出し範囲を ``[start, end)`` にする ``None`` なら解除して全体へ戻す

    タイムラインの長さより後ろまで決めてよい クリップを後から伸ばしたときに、
    決めた範囲が勝手に縮むと、見えていた帯と書き出しが食い違う
    はみ出した分は書き出すときに切る（:func:`export_range`）
    """

    work_area: tuple[int, int] | None

    @property
    def label(self) -> str:
        return "書き出し範囲を解除" if self.work_area is None else "書き出し範囲を指定"

    def apply(self, project: Project) -> Project:
        if self.work_area is not None:
            start, end = self.work_area
            if start < 0:
                raise ValueError(f"書き出し範囲の始まりが負: {start}")
            if end <= start:
                raise ValueError(f"書き出し範囲が空: {self.work_area}")
        return project.with_timeline(replace(project.timeline, work_area=self.work_area))


def export_range(timeline: Timeline) -> tuple[int, int] | None:
    """範囲のうち、書き出して中身のある所 ``[start, end)``

    範囲が無い・範囲がすべてタイムラインの終わりより後ろなら ``None``
    終わりより後ろまでそのまま書き出すと、何も映らない黒と無音が後ろに付く
    """
    if timeline.work_area is None:
        return None
    start, end = timeline.work_area
    end = min(end, timeline.duration)
    return (start, end) if end > start else None
