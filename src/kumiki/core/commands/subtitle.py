"""字幕の編集コマンドと、タイムラインへの焼き込み。

字幕は素材（:class:`~kumiki.core.model.MediaItem`）に属するので、ここの操作は
どれもタイムラインを触らない。1 か所直せば、その素材を使っているすべての箇所に
同時に反映される。素材を 3 回置いていても、直すのは 1 回で済む。

例外は :func:`burn_subtitles` で、これだけはタイムラインへテキストを並べる。
書き出し先が字幕に対応していない場合や、装飾を凝りたい場合の逃げ道。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from fractions import Fraction

from kumiki.core.commands.base import Command
from kumiki.core.commands.edit import AddClip, AddTrack
from kumiki.core.model import (
    Clip,
    GeneratedSource,
    MediaId,
    Project,
    SegmentId,
    Track,
    TrackKind,
    Transcript,
    TranscriptSegment,
)
from kumiki.core.projection import project_timeline

__all__ = [
    "AddSegment",
    "MergeWithNext",
    "RemoveSegment",
    "RetimeSegment",
    "SetSegmentText",
    "SplitSegment",
    "burn_subtitles",
]

#: 焼き込むテキストオブジェクトで、本文を入れるパラメータ名。
TEXT_PARAM = "text"


@dataclass(frozen=True, slots=True)
class SetSegmentText(Command):
    """字幕 1 枚の本文を書き換える。"""

    media_id: MediaId
    segment_id: SegmentId
    text: str

    @property
    def label(self) -> str:
        return "字幕を編集"

    def apply(self, project: Project) -> Project:
        transcript, segment = _locate(project, self.media_id, self.segment_id)
        updated = transcript.replace_segment(segment.with_text(self.text))
        return _store(project, self.media_id, updated)


@dataclass(frozen=True, slots=True)
class RetimeSegment(Command):
    """字幕 1 枚の時刻を動かす。値はソース秒。"""

    media_id: MediaId
    segment_id: SegmentId
    start: Fraction
    end: Fraction

    @property
    def label(self) -> str:
        return "字幕の時刻を変更"

    def apply(self, project: Project) -> Project:
        transcript, segment = _locate(project, self.media_id, self.segment_id)
        if self.end <= self.start:
            raise ValueError(f"終了が開始以前: {self.start} .. {self.end}")
        moved = replace(segment, start=self.start, end=self.end, edited=True)
        return _store(project, self.media_id, _rebuilt(transcript, _swap(transcript, moved)))


@dataclass(frozen=True, slots=True)
class RemoveSegment(Command):
    """字幕 1 枚を消す。"""

    media_id: MediaId
    segment_id: SegmentId

    @property
    def label(self) -> str:
        return "字幕を削除"

    def apply(self, project: Project) -> Project:
        transcript, segment = _locate(project, self.media_id, self.segment_id)
        remaining = [s for s in transcript.segments if s.id != segment.id]
        return _store(project, self.media_id, _rebuilt(transcript, remaining))


@dataclass(frozen=True, slots=True)
class AddSegment(Command):
    """字幕を 1 枚足す。起こしを使わずに手で入れる場合に使う。"""

    media_id: MediaId
    start: Fraction
    end: Fraction
    text: str = ""

    @property
    def label(self) -> str:
        return "字幕を追加"

    def apply(self, project: Project) -> Project:
        item = project.require_media(self.media_id)
        transcript = item.transcript if item.transcript is not None else Transcript()
        added = TranscriptSegment(start=self.start, end=self.end, text=self.text, edited=True)
        return _store(project, self.media_id, _rebuilt(transcript, [*transcript.segments, added]))


@dataclass(frozen=True, slots=True)
class SplitSegment(Command):
    """字幕 1 枚を、ソース時刻 ``at`` で 2 枚に割る。

    単語タイムスタンプがあればその境界で本文を分ける。無ければ時間の比で分ける。
    どちらにしても文の途中で切れることはあるので、割ったあとに手で直せるよう、
    分けた両方に編集済みの印は付けない。
    """

    media_id: MediaId
    segment_id: SegmentId
    at: Fraction

    @property
    def label(self) -> str:
        return "字幕を分割"

    def apply(self, project: Project) -> Project:
        transcript, segment = _locate(project, self.media_id, self.segment_id)
        if not (segment.start < self.at < segment.end):
            raise ValueError(f"分割位置が字幕の内側にない: {self.at}")

        head_text, tail_text = _split_text(segment, self.at)
        head = TranscriptSegment(
            start=segment.start,
            end=self.at,
            text=head_text,
            words=tuple(w for w in segment.words if w.start < self.at),
            speaker=segment.speaker,
            id=segment.id,
        )
        tail = TranscriptSegment(
            start=self.at,
            end=segment.end,
            text=tail_text,
            words=tuple(w for w in segment.words if w.start >= self.at),
            speaker=segment.speaker,
        )
        others = [s for s in transcript.segments if s.id != segment.id]
        return _store(project, self.media_id, _rebuilt(transcript, [*others, head, tail]))


@dataclass(frozen=True, slots=True)
class MergeWithNext(Command):
    """字幕を次の 1 枚と繋げる。認識が細かく割れすぎたときに使う。"""

    media_id: MediaId
    segment_id: SegmentId

    @property
    def label(self) -> str:
        return "字幕を結合"

    def apply(self, project: Project) -> Project:
        transcript, segment = _locate(project, self.media_id, self.segment_id)
        index = transcript.segments.index(segment)
        if index + 1 >= len(transcript.segments):
            raise ValueError("次の字幕が無い")
        following = transcript.segments[index + 1]

        parts = [part for part in (segment.text.strip(), following.text.strip()) if part]
        merged = TranscriptSegment(
            start=segment.start,
            end=following.end,
            # 改行ではなく空白で繋ぐ。折り返しは整形が決めるもので、ここで行を
            # 確定させると、整形を掛け直したときに二重に折り返される。
            text=" ".join(parts),
            words=segment.words + following.words,
            speaker=segment.speaker or following.speaker,
            edited=True,
            id=segment.id,
        )
        remaining = [s for s in transcript.segments if s.id not in (segment.id, following.id)]
        return _store(project, self.media_id, _rebuilt(transcript, [*remaining, merged]))


def burn_subtitles(
    project: Project,
    template: GeneratedSource,
    *,
    track_name: str = "字幕",
    text_param: str = TEXT_PARAM,
) -> list[Command]:
    """いま画面に出る字幕を、テキストオブジェクトとしてタイムラインへ並べる。

    投影した結果をそのまま置くので、この時点のカット状態が固定される。あとから
    素材を切っても焼き込んだテキストは動かない。だから仕上げの最後に使う。

    重なる字幕は前の方を切り詰める。1 本のトラックにクリップを重ねられないため。
    重ねたい場合は、焼き込む前に字幕側を整理する。
    """
    projected = list(project_timeline(project))
    if not projected:
        return []

    track = Track(kind=TrackKind.VIDEO, name=track_name)
    commands: list[Command] = [AddTrack(track)]

    placed: list[tuple[int, int, str]] = []
    for subtitle in projected:
        text = subtitle.segment.text.strip()
        if not text:
            continue
        start, end = subtitle.start_frame, subtitle.end_frame
        if placed and start < placed[-1][1]:
            previous_start, _, previous_text = placed[-1]
            if start <= previous_start:
                continue
            placed[-1] = (previous_start, start, previous_text)
        placed.append((start, end, text))

    for start, end, text in placed:
        commands.append(
            AddClip(
                track.id,
                Clip(
                    timeline_start=start,
                    duration=end - start,
                    source=template.with_param(text_param, text),
                ),
            )
        )
    return commands if len(commands) > 1 else []


def _locate(
    project: Project, media_id: MediaId, segment_id: SegmentId
) -> tuple[Transcript, TranscriptSegment]:
    item = project.require_media(media_id)
    if item.transcript is None:
        raise KeyError(f"素材に字幕が無い: {item.name}")
    for segment in item.transcript.segments:
        if segment.id == segment_id:
            return item.transcript, segment
    raise KeyError(f"字幕が見つからない: {segment_id}")


def _swap(transcript: Transcript, segment: TranscriptSegment) -> list[TranscriptSegment]:
    return [segment if s.id == segment.id else s for s in transcript.segments]


def _rebuilt(transcript: Transcript, segments: list[TranscriptSegment]) -> Transcript:
    """順序を整えて作り直す。

    :class:`Transcript` は開始時刻の昇順を不変条件にしている。時刻を動かす操作で
    並びが崩れるので、呼び出し側が気にせずに済むようここで整える。
    """
    ordered = sorted(segments, key=lambda s: (s.start, s.end))
    return Transcript(tuple(ordered), language=transcript.language, model=transcript.model)


def _store(project: Project, media_id: MediaId, transcript: Transcript) -> Project:
    item = project.require_media(media_id)
    return project.replace_media(item.with_transcript(transcript))


def _split_text(segment: TranscriptSegment, at: Fraction) -> tuple[str, str]:
    """本文を分割位置で分ける。"""
    text = segment.text.strip()
    if segment.words:
        head = "".join(w.text for w in segment.words if w.start < at).strip()
        tail = "".join(w.text for w in segment.words if w.start >= at).strip()
        if head or tail:
            return head, tail

    if segment.duration <= 0:
        return text, ""
    ratio = (at - segment.start) / segment.duration
    cut = max(0, min(len(text), round(len(text) * float(ratio))))
    return text[:cut].strip(), text[cut:].strip()
