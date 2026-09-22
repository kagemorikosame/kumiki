"""編集で、どのフレームの絵が変わったかを求める

先読みした絵を捨てる範囲を決めるために使う 編集のたびに全部捨てると、
先読みは「編集していない間だけ効く仕組み」になってしまう 1 本のクリップを
触ったときに、離れた場所の絵まで作り直すのは無駄

**足りない方へ倒さない** 変わったのに捨て損ねると、古い絵が画面に残る
判断が付かないものは :meth:`Invalidation.all` にする（作り直すだけで済む）
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass, replace

from sashimono.core.model import (
    Clip,
    MediaId,
    MediaItem,
    Project,
    SceneId,
    Timeline,
    Track,
    TrackKind,
)
from sashimono.effects import FileSpec, registry

__all__ = ["Invalidation", "changed_spans", "image_paths", "image_spans"]


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
        if _visual_track(old) != _visual_track(new):
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
        if first is not None and second is not None and _visual(first) == _visual(second):
            continue
        spans.extend(_span(clip) for clip in (first, second) if clip is not None)
    return spans


def _visual_track(track: Track) -> Track:
    """見た目の都合と音の設定を外したトラック クリップは別に比べるので外す

    名前・鍵・行の高さ・音量・定位はどれもレンダラが読まない
    トラックに名前を付け直すたびに、その上のクリップの絵を全部捨てていた

    **外すのはこれだけ** トラック全体のフィルタ（:attr:`Track.effects`）は
    いまのレンダラが読んでいないが、比べる側に残す 読むようになったときに、
    黙って古い絵が出る形にはしない
    """
    return replace(track, clips=(), name="", locked=False, height=0, volume_db=0.0, pan=0.0)


def _visual(clip: Clip) -> Clip:
    """束ねとリンクを外したクリップ 絵が同じかどうかを比べるため

    どちらもレンダラが読まない 束ね直すたびに貯めた絵を捨てると、
    並べ終えた後の整理でプレビューが作り直しになる

    **外すのはこの 2 つだけ** 新しく足した項目は比べる側に入る（絵に出ないと
    分かってから外す）捨てそこなうより、捨てすぎる方がまだ直しやすい
    """
    return replace(clip, link_group=None, group_id=None)


def _span(clip: Clip) -> tuple[int, int]:
    return clip.timeline_start, clip.timeline_end


def _depends_on(clip: Clip, scenes: set[SceneId], media: set[MediaId]) -> bool:
    """そのクリップが、中身の変わったシーンか素材を読んでいるか"""
    if clip.scene_id is not None and clip.scene_id in scenes:
        return True
    return clip.media_id is not None and clip.media_id in media


def _media_used(timeline: Timeline) -> set[MediaId]:
    """そのタイムラインが**絵のために**読んでいる素材

    音のトラックは数えない 音だけに使っている素材を差し替えても絵は変わらない
    """
    return {
        clip.media_id
        for track in timeline.active_tracks(TrackKind.VIDEO)
        for clip in track.clips
        if clip.media_id is not None
    }


def _picture_of_timeline(timeline: Timeline) -> tuple[object, ...]:
    """そのタイムラインのうち、絵を決める所だけ

    シーンの中身が変わったかを見るのに使う タイムラインを丸ごと比べると、
    シーンの中で音量を動かしたりクリップを束ね直したりしただけで、
    そのシーンを置いた所の絵を全部捨てることになる メインのタイムラインでは
    同じものを無視しているので、シーンだけ扱いが違うのはつじつまが合わない
    """
    return tuple(
        (track.id, _visual_track(track), tuple(_visual(clip) for clip in track.clips))
        for track in timeline.active_tracks(TrackKind.VIDEO)
    )


def _changed_media(before: Project, after: Project) -> set[MediaId]:
    """**絵に関わる所**が変わった素材 差し替えと読み込み直しで絵が変わる

    素材そのものを比べない :class:`MediaItem` は字幕の起こし結果や表示名も
    持っていて、そちらはレンダラが読まない 丸ごと比べると、字幕を 1 文字
    直すたびにその素材を使う所の先読みが全部消える（字幕を焼き込む使い方では、
    貯める意味がなくなる）
    """
    old = {item.id: _picture_of(item) for item in before.media}
    new = {item.id: _picture_of(item) for item in after.media}
    return {key for key in old.keys() | new.keys() if old.get(key) != new.get(key)}


def _picture_of(media: MediaItem) -> tuple[object, ...]:
    """その素材のうち、絵を決める所だけ

    音の流れそのものは混ぜる側の持ち物で、絵には出ない ただし
    :attr:`~sashimono.core.model.MediaItem.is_still` は音の有無でも変わり、
    レンダラはこれを見て「常に先頭のコマを返す」へ切り替える
    長さ 0 の映像素材に音が付いたかどうかで絵が変わるので、そこだけ拾う
    """
    return (media.path, media.duration, media.video_streams, media.is_still)


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
    pictures = ({k: _picture_of_timeline(t) for k, t in side.items()} for side in (old, new))
    old_picture, new_picture = pictures
    changed = {
        key for key in old.keys() | new.keys() if old_picture.get(key) != new_picture.get(key)
    }
    changed |= {
        scene_id
        for timelines in (old, new)
        for scene_id, timeline in timelines.items()
        if _media_used(timeline) & media
    }

    return _with_containers(changed, old, new)


def _with_containers(changed: set[SceneId], *sides: dict[SceneId, Timeline]) -> set[SceneId]:
    """変わったシーンと、それを（入れ子で）置いているシーン

    変わったシーンを置いているシーンも、外から見れば変わっている
    数えきるまで繰り返す（シーンの入れ子は循環しないことがモデル側で保証されている）
    """
    changed = set(changed)
    while True:
        spread = {
            scene_id
            for timelines in sides
            for scene_id, timeline in timelines.items()
            if scene_id not in changed and timeline.scene_references() & changed
        }
        if not spread:
            return changed
        changed |= spread


def image_spans(project: Project, paths: Collection[str]) -> Invalidation:
    """エフェクトが読む画像（画像合成の絵、縁取りの模様）が書き換わったときに捨てる範囲

    画像はパスで指すだけなので、ファイルを書き換えてもプロジェクトは変わらない
    編集の差分（:func:`changed_spans`）には出てこないので、別に求める
    シーンの中で使っていれば、そのシーンを置いたクリップ全体を捨てる
    """
    wanted = frozenset(path for path in paths if path)
    if not wanted:
        return Invalidation.nothing()
    timelines = {scene.id: scene.timeline for scene in project.scenes}
    scenes = _with_containers(
        {key for key, timeline in timelines.items() if _timeline_reads(timeline, wanted)},
        timelines,
    )
    return Invalidation.over(
        _span(clip)
        for track in project.timeline.active_tracks(TrackKind.VIDEO)
        for clip in track.clips
        if _reads_image(clip, wanted) or (clip.scene_id is not None and clip.scene_id in scenes)
    )


def _timeline_reads(timeline: Timeline, paths: frozenset[str]) -> bool:
    return any(
        _reads_image(clip, paths)
        for track in timeline.active_tracks(TrackKind.VIDEO)
        for clip in track.clips
    )


def _reads_image(clip: Clip, paths: frozenset[str]) -> bool:
    """そのクリップのエフェクトが ``paths`` のどれかを画像として読むか"""
    return not _images_of(clip).isdisjoint(paths)


def _images_of(clip: Clip) -> set[str]:
    """そのクリップのエフェクトが画像として読むパス

    切ってあるエフェクトも数える 捨てすぎても作り直すだけで済むが、
    切り替えの途中で見落とすと古い絵が残る

    場面切り替えの後の場面に掛けるエフェクト（``after_effects``）も見る
    前の場面だけを見ると、後の場面の模様を描き直しても古い絵が残る
    """
    found: set[str] = set()
    for effect in (*clip.effects, *clip.after_effects):
        definition = registry.get(effect.kind)
        if definition is None:
            continue
        for spec in definition.parameters:
            value = effect.params.get(spec.name)
            if isinstance(spec, FileSpec) and spec.texture and isinstance(value, str) and value:
                found.add(value)
    return found


def image_paths(project: Project) -> frozenset[str]:
    """プロジェクトのどこか（シーンの中も）で、エフェクトが画像として読むパス

    これに無い画像は GPU から手放してよい 見えないトラックの物も残す
    トラックを表示へ戻すたびに読み直すことになる
    """
    return frozenset(
        path
        for timeline in (project.timeline, *(scene.timeline for scene in project.scenes))
        for track in timeline.tracks
        for clip in track.clips
        for path in _images_of(clip)
    )
