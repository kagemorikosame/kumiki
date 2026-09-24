"""プロジェクトの形（フレームレート・解像度）を、最初に置く動画へ合わせる

フレームレートは、クリップを置いた後は変えない クリップの位置と長さはフレームの数で
持っているので、変えると全部を数え直すことになり、丸めで 1 フレームずつずれていく
（:mod:`sashimono.ui.project_settings_dialog` もこの理由で作った後は選ばせない）
タイムラインが空なら数え直すクリップが無いので、そのときだけ変えてよい
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from fractions import Fraction

from sashimono.core.commands.base import Command
from sashimono.core.commands.edit import MAX_RESOLUTION, MIN_RESOLUTION, SetResolution
from sashimono.core.model import MediaItem, Project, Timeline
from sashimono.core.timebase import FrameRate, Rounding, frame_to_seconds, seconds_to_frame

__all__ = [
    "SetFrameRate",
    "VideoFormat",
    "format_to_match",
    "is_blank",
    "match_commands",
    "rate_for_project",
    "video_format",
]

#: 決まったフレームレート（:meth:`FrameRate.from_decimal` の吸着先）から、これだけ離れていても
#: そちらへ寄せる 可変フレームレートの動画（スマホの撮影など）は平均のレートが 29.87 の
#: ような半端な値で書かれている そのままプロジェクトにすると、書き出しまで 29.87 fps になる
_VARIABLE_RATE_TOLERANCE = Fraction(2, 100)

#: 可変フレームレートの寄せ先 整数のレートだけにする 29.97 の動画は平均でもほぼ
#: 30000/1001 ちょうどに出るので、手前の吸着（:meth:`FrameRate.from_decimal`）で決まる
#: ここへ来るのは 29.87 のような半端な平均で、スマホは 30 を狙って撮っている
#: 29.97 も候補に入れると、30 を狙った動画が近い方の 29.97 へ寄ってしまう
_STANDARD_RATES: tuple[FrameRate, ...] = (
    FrameRate(24),
    FrameRate(25),
    FrameRate(30),
    FrameRate(50),
    FrameRate(60),
    FrameRate(120),
)

#: 分数のまま持つ NTSC 系のレート 吸着でここへ来たものは寄せ直さない
_NTSC_RATES = frozenset(Fraction(n * 1000, 1001) for n in (24, 30, 48, 60, 120))


@dataclass(frozen=True, slots=True)
class VideoFormat:
    """プロジェクトを合わせる先 解像度（偶数）とフレームレート（分数のまま）"""

    width: int
    height: int
    rate: FrameRate


def rate_for_project(rate: FrameRate) -> FrameRate:
    """動画のフレームレートを、プロジェクトに使う値へ

    29.97 は分数（30000/1001）のまま返す 小数へ丸めると 1 時間で 3 フレーム以上ずれる
    決まったレートの近く（2% 以内）にあれば、そのレートへ寄せる 可変フレームレートの
    動画は平均の値しか分からず、半端な数のままにすると、書き出しもその半端なレートになる
    """
    exact = FrameRate.from_decimal(rate.fps)
    if exact.fps in _NTSC_RATES:
        return exact
    nearest = min(_STANDARD_RATES, key=lambda candidate: abs(candidate.fps - exact.fps))
    if abs(nearest.fps - exact.fps) / nearest.fps <= _VARIABLE_RATE_TOLERANCE:
        return nearest
    return exact


def _even(value: int) -> int:
    """書き出しの yuv420p は 2 画素ごとに色を持つので、奇数の大きさは 1 つ上の偶数へ"""
    clamped = min(max(value, MIN_RESOLUTION), MAX_RESOLUTION)
    return min(clamped + clamped % 2, MAX_RESOLUTION)


def video_format(media: MediaItem) -> VideoFormat | None:
    """動画の形 絵の無い素材と静止画は ``None``

    回転の入った動画（スマホの縦撮り）は、回した後の大きさを使う 回す前の大きさに
    合わせると、縦の動画が横長のプロジェクトの中で小さく映る
    """
    if not media.video_streams or media.is_still or media.duration <= 0:
        return None
    stream = media.video_streams[0]
    width, height = stream.display_size
    return VideoFormat(_even(width), _even(height), rate_for_project(stream.frame_rate))


def is_blank(project: Project) -> bool:
    """メインにもシーンにもクリップが 1 本も無いか

    シーンの中にだけクリップがあっても空とはみなさない シーンのタイムラインも
    プロジェクトと同じフレームレートで数えているので、変えると数え直しになる
    """
    return not any(
        track.clips
        for timeline in (project.timeline, *(scene.timeline for scene in project.scenes))
        for track in timeline.tracks
    )


def format_to_match(project: Project, media: Iterable[MediaItem]) -> VideoFormat | None:
    """空のプロジェクトへ置こうとしている素材のうち、最初の動画の形 合っていれば ``None``

    置くのが 2 本目以降（タイムラインが空でない）なら尋ねない 置いた後に変えると、
    それまでのクリップの長さが変わる
    """
    if not is_blank(project):
        return None
    for item in media:
        found = video_format(item)
        if found is None:
            continue
        settings = project.settings
        if found.rate == settings.frame_rate and (found.width, found.height) == (
            settings.width,
            settings.height,
        ):
            return None
        return found
    return None


def match_commands(project: Project, target: VideoFormat) -> list[Command]:
    """プロジェクトを ``target`` へ合わせるコマンド 同じ所は変えない"""
    commands: list[Command] = []
    if target.rate != project.settings.frame_rate:
        commands.append(SetFrameRate(target.rate))
    if (target.width, target.height) != project.settings.resolution:
        commands.append(SetResolution(target.width, target.height))
    return commands


def _retimed(timeline: Timeline, old: FrameRate, new: FrameRate) -> Timeline:
    """目印と書き出し範囲を、同じ時刻（秒）のまま新しいフレームレートで数え直す

    クリップは無い（:class:`SetFrameRate` が確かめている） 目印と範囲だけは空の
    タイムラインにも置けるので、フレームの数のまま持ち越すと、置いた時刻からずれる
    """

    def convert(frame: int) -> int:
        return seconds_to_frame(frame_to_seconds(frame, old), new, Rounding.NEAREST)

    markers = tuple(replace(marker, frame=convert(marker.frame)) for marker in timeline.markers)
    work_area = timeline.work_area
    if work_area is not None:
        start, end = convert(work_area[0]), convert(work_area[1])
        # 短い範囲が丸めで 0 フレームに潰れると、範囲そのものを作れない
        work_area = (start, max(end, start + 1))
    return replace(timeline, rate=new, markers=markers, work_area=work_area)


@dataclass(frozen=True, slots=True)
class SetFrameRate(Command):
    """プロジェクトのフレームレートを変える メインにもシーンにもクリップが無いときだけ

    シーンのタイムラインも一緒に変える プロジェクトとシーンでレートが違うと、
    置いたシーンの時刻を換算することになる（:class:`Project` が断る）
    """

    rate: FrameRate

    @property
    def label(self) -> str:
        return f"フレームレートを変更: {self.rate} fps"

    def apply(self, project: Project) -> Project:
        if not is_blank(project):
            raise ValueError("クリップを置いた後はフレームレートを変えられない")
        old = project.settings.frame_rate
        if self.rate == old:
            return project
        scenes = tuple(
            replace(scene, timeline=_retimed(scene.timeline, old, self.rate))
            for scene in project.scenes
        )
        return replace(
            project,
            settings=replace(project.settings, frame_rate=self.rate),
            timeline=_retimed(project.timeline, old, self.rate),
            scenes=scenes,
        )
