"""タイムライン、トラック、クリップ

クリップの位置と長さは**フレーム単位の整数**で持つ ここを秒にすると、隣接クリップの
境界で丸め方向が食い違って 1 フレームの隙間や重なりが生まれる 素材のどこを使うかを
示す ``source_in`` だけは秒（:class:`~fractions.Fraction`）で持つ 素材のフレームレートが
プロジェクトと異なることがあるため
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from enum import Enum
from fractions import Fraction

from sashimono.core.model.effect import AnimatedValue, Effect, ParamValue
from sashimono.core.model.ids import (
    ClipId,
    GroupId,
    MediaId,
    SceneId,
    TrackId,
    new_clip_id,
    new_track_id,
)
from sashimono.core.timebase import FrameRate

__all__ = ["Clip", "GeneratedSource", "Marker", "Timeline", "Track", "TrackKind"]


@dataclass(frozen=True, slots=True)
class GeneratedSource:
    """素材を持たないクリップの中身 テキストや図形

    :class:`~sashimono.core.model.Effect` と同じく ``kind`` と ``params`` だけを持つ
    フィルタと生成物は役割が違うので型は分けるが、パラメータの仕組みは共有する
    設定 UI もプリセットも 1 つの実装で済ませるため
    """

    kind: str
    params: dict[str, ParamValue] = field(default_factory=dict)

    def with_param(self, name: str, value: ParamValue) -> GeneratedSource:
        return GeneratedSource(kind=self.kind, params={**self.params, name: value})


class TrackKind(Enum):
    VIDEO = "video"
    AUDIO = "audio"


@dataclass(frozen=True, slots=True)
class Clip:
    """タイムライン上に置かれた 1 つのクリップ

    ``media_id`` が ``None`` のクリップは、素材を持たない生成オブジェクト
    （テキスト、図形など） その場合の見た目は :attr:`effects` が決める
    ``scene_id`` を持つクリップは、別のシーン（タイムライン）を 1 本の絵と音として
    入れ子に置いたもの（AviUtl のシーンオブジェクト）
    """

    #: タイムライン上の開始位置（フレーム）
    timeline_start: int
    #: タイムライン上の長さ（フレーム） 1 以上
    duration: int
    media_id: MediaId | None = None
    #: 素材を持たないクリップの中身（テキスト・図形） ``media_id`` が
    #: ``None`` のときだけ意味を持つ
    source: GeneratedSource | None = None
    #: 素材内の開始位置（秒）
    source_in: Fraction = Fraction(0)
    #: 使用する素材内のストリーム番号 多言語音声などで意味を持つ
    stream_index: int = 0
    #: 再生速度 2 なら 2 倍速で、同じ長さに 2 倍のソース範囲が入る
    speed: Fraction = Fraction(1)
    #: 絵を止める素材の時刻（秒） 読む時刻がここを越えたら、ここの絵を出し続ける
    #: ``None`` なら止めない（素材の終わりを越えた所は何も映らない）
    #:
    #: 素材の長さを越えた所で最後の絵を出し続ける（YMM4 の素材より長い動画アイテム）なら
    #: 最後のフレームの時刻、頭の絵で止める（YMM4 の再生速度 0）なら ``source_in`` を持つ
    #: 「止める時刻」ではなく**素材の中の時刻の上限**で持つのは、分割・トリム・速さの
    #: 変更で ``source_in`` と ``speed`` が変わっても、そのまま写すだけで同じ絵が出るため
    #: 分割した後半が上限より後から始まれば、後半はずっと止まった絵になる
    #: クリップの中の経過で持つと、編集の命令がそれぞれ計算し直す必要があり、1 つでも
    #: 忘れると止まる位置がずれる
    #:
    #: **絵だけに効く** 音は止めない（止めた絵の間も素材は進む） YMM4 でも、素材の
    #: 終わりを越えた所は無音で、再生速度 0 の音は鳴らない（音量 0 で写している）
    hold_at: Fraction | None = None
    effects: tuple[Effect, ...] = ()
    #: 場面切り替え（生成オブジェクト ``transition``）で、後の場面に掛けるエフェクト
    #: 前の場面には :attr:`effects` が掛かる ほかのクリップでは使わない
    after_effects: tuple[Effect, ...] = ()
    opacity: AnimatedValue = field(default_factory=lambda: AnimatedValue(1.0))
    #: 下のトラックとの重ね方 値は :class:`~sashimono.engine.gpu.BlendMode` の定数
    #: 文字列で持つのは、プロジェクトファイルに出るものを列挙型に縛らないため
    blend_mode: str = "normal"
    #: すぐ下に重なっているクリップの形（不透明度）で切り抜く YMM4 の「上のオブジェクトで
    #: クリッピング」 背景の模様を吹き出しの形だけに見せる、といった使い方をする
    clip_to_below: bool = False
    #: 映像と音声を連動させるためのグループ 同じ値を持つクリップは一緒に動く
    link_group: GroupId | None = None
    #: 入れ子にしたシーン 素材（``media_id``）とは同時に持てない
    scene_id: SceneId | None = None
    #: 束ねたグループ 同じ値を持つクリップは、クリック 1 回でまとめて選ばれる
    #: ``link_group`` とは別物 リンクは映像と音声の同期、グループは編集の手間を
    #: 省くための束ねで、解除しても同期は崩れない
    group_id: GroupId | None = None
    enabled: bool = True
    id: ClipId = field(default_factory=new_clip_id)

    def __post_init__(self) -> None:
        if self.duration <= 0:
            raise ValueError(f"クリップの長さは 1 フレーム以上必要: {self.duration}")
        if self.source_in < 0:
            raise ValueError(f"素材内の開始位置が負: {self.source_in}")
        if self.speed <= 0:
            raise ValueError(f"再生速度は正でなければならない: {self.speed}")
        if self.hold_at is not None and self.hold_at < 0:
            raise ValueError(f"絵を止める時刻が負: {self.hold_at}")
        if self.scene_id is not None and (self.media_id is not None or self.source is not None):
            # 両方を持つと、どちらを描くのかが決まらない
            raise ValueError("シーンを置いたクリップは素材や生成オブジェクトを持てない")

    @property
    def timeline_end(self) -> int:
        """タイムライン上の終了位置（フレーム、この位置は含まない）"""
        return self.timeline_start + self.duration

    def source_duration(self, rate: FrameRate) -> Fraction:
        """このクリップが素材から消費するソース時間の長さ（秒）"""
        return self.duration * rate.frame_duration * self.speed

    def source_out(self, rate: FrameRate) -> Fraction:
        """素材内の終了位置（秒、この位置は含まない）"""
        return self.source_in + self.source_duration(rate)

    def picture_time(self, local_frame: int, rate: FrameRate) -> Fraction:
        """クリップの頭から ``local_frame`` 進んだ所で、絵を素材のどの時刻から取るか

        絵を止めていれば :attr:`hold_at` を越えない 音はこれを使わない（止めない）
        描画・先読み・タイムラインの絵の並びが同じ式を使う ずれると先読みが当たらず、
        タイムラインに並ぶ絵とプレビューが食い違う
        """
        seconds = self.source_in + local_frame * rate.frame_duration * self.speed
        if self.hold_at is not None and seconds > self.hold_at:
            return self.hold_at
        return seconds

    def contains(self, frame: int) -> bool:
        return self.timeline_start <= frame < self.timeline_end

    def overlaps(self, start: int, end: int) -> bool:
        """タイムライン範囲 ``[start, end)`` と重なるか"""
        return self.timeline_start < end and start < self.timeline_end

    def moved_to(self, timeline_start: int) -> Clip:
        """ソース範囲を保ったまま、タイムライン上の位置だけ変えた複製を返す"""
        return replace(self, timeline_start=timeline_start)


@dataclass(frozen=True, slots=True)
class Marker:
    """タイムライン上の目印"""

    frame: int
    label: str = ""
    color: str = "#ffcc00"


@dataclass(frozen=True, slots=True)
class Track:
    """クリップを並べる 1 本のトラック

    同一トラック内でクリップが重なることは許さない 重なりを許すと「どちらが上か」の
    規則が必要になり、リップル編集の意味も定義できなくなる 重ねたい場合は
    トラックを分ける
    """

    kind: TrackKind
    name: str = ""
    clips: tuple[Clip, ...] = ()
    #: トラック全体に掛かるフィルタ（AviUtl のフィルタオブジェクト相当）
    effects: tuple[Effect, ...] = ()
    locked: bool = False
    muted: bool = False
    solo: bool = False
    #: UI 上の表示高さ（ピクセル）
    height: int = 60
    #: 音声トラックの音量（dB） 映像トラックでは無視される
    volume_db: float = 0.0
    #: 音声トラックの定位 -1 が左、+1 が右
    pan: float = 0.0
    id: TrackId = field(default_factory=new_track_id)

    def __post_init__(self) -> None:
        starts = [c.timeline_start for c in self.clips]
        if starts != sorted(starts):
            raise ValueError(f"トラック {self.name!r} のクリップが開始位置順に並んでいない")
        for left, right in zip(self.clips, self.clips[1:], strict=False):
            if left.timeline_end > right.timeline_start:
                raise ValueError(
                    f"トラック {self.name!r} でクリップが重なっている: "
                    f"{left.id} [{left.timeline_start}, {left.timeline_end}) と "
                    f"{right.id} [{right.timeline_start}, {right.timeline_end})"
                )

    @property
    def end_frame(self) -> int:
        """最後のクリップの終端 空トラックなら 0"""
        return self.clips[-1].timeline_end if self.clips else 0

    def clip_at(self, frame: int) -> Clip | None:
        """``frame`` にあるクリップ 無ければ ``None``"""
        for clip in self.clips:
            if clip.contains(frame):
                return clip
        return None

    def find(self, clip_id: ClipId) -> Clip | None:
        for clip in self.clips:
            if clip.id == clip_id:
                return clip
        return None

    def with_clips(self, clips: tuple[Clip, ...]) -> Track:
        """クリップ列を差し替えた複製を返す 開始位置順に整列してから渡す"""
        return replace(self, clips=tuple(sorted(clips, key=lambda c: c.timeline_start)))


@dataclass(frozen=True, slots=True)
class Timeline:
    """トラックの集合"""

    rate: FrameRate
    tracks: tuple[Track, ...] = ()
    markers: tuple[Marker, ...] = ()
    #: 書き出し範囲 ``None`` なら全体
    work_area: tuple[int, int] | None = None

    def __post_init__(self) -> None:
        ids = [t.id for t in self.tracks]
        if len(set(ids)) != len(ids):
            raise ValueError("トラック ID が重複している")
        if self.work_area is not None:
            start, end = self.work_area
            if end <= start:
                raise ValueError(f"書き出し範囲が不正: {self.work_area}")

    @property
    def duration(self) -> int:
        """全トラックを通した長さ（フレーム）"""
        return max((t.end_frame for t in self.tracks), default=0)

    def video_tracks(self) -> Iterator[Track]:
        return (t for t in self.tracks if t.kind is TrackKind.VIDEO)

    def audio_tracks(self) -> Iterator[Track]:
        return (t for t in self.tracks if t.kind is TrackKind.AUDIO)

    def active_tracks(self, kind: TrackKind) -> tuple[Track, ...]:
        """実際に映る／聞こえるトラック 並びは :attr:`tracks` のまま

        ミュートを除き、同じ種類にソロが 1 本でもあればソロのものだけを残す
        ミュートとソロが両方付いていればミュートが勝ち、そのソロはほかを止めない
        止めると、ソロを外し忘れたトラックをミュートしただけで全部が無音になる

        プレビュー・ミキサ・書き出しの 3 か所が必ずここを通る 判断が分かれると
        「プレビューでは消えているのに書き出すと出る」が起きる 実際に書き出しだけ
        ソロを見ていなかった
        """
        tracks = [t for t in self.tracks if t.kind is kind]
        soloed = any(t.solo and not t.muted for t in tracks)
        return tuple(t for t in tracks if not t.muted and (t.solo or not soloed))

    def find_track(self, track_id: TrackId) -> Track | None:
        for track in self.tracks:
            if track.id == track_id:
                return track
        return None

    def locate_clip(self, clip_id: ClipId) -> tuple[Track, Clip] | None:
        """クリップとその所属トラックを探す"""
        for track in self.tracks:
            clip = track.find(clip_id)
            if clip is not None:
                return track, clip
        return None

    def replace_track(self, track: Track) -> Timeline:
        """同じ ID のトラックを差し替えた新しい :class:`Timeline` を返す"""
        for index, existing in enumerate(self.tracks):
            if existing.id == track.id:
                tracks = (*self.tracks[:index], track, *self.tracks[index + 1 :])
                return replace(self, tracks=tracks)
        raise KeyError(f"トラックが見つからない: {track.id}")

    def linked_clips(self, group: GroupId) -> Iterator[tuple[Track, Clip]]:
        """同じリンクグループに属するクリップをすべて返す"""
        for track in self.tracks:
            for clip in track.clips:
                if clip.link_group == group:
                    yield track, clip

    def grouped_clips(self, group: GroupId) -> Iterator[tuple[Track, Clip]]:
        """同じグループ（束ね）に属するクリップをすべて返す"""
        for track in self.tracks:
            for clip in track.clips:
                if clip.group_id == group:
                    yield track, clip

    def scene_references(self) -> set[SceneId]:
        """このタイムラインに置かれているシーン"""
        return {
            clip.scene_id
            for track in self.tracks
            for clip in track.clips
            if clip.scene_id is not None
        }
