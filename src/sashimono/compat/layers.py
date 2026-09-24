"""互換の読み込みを、混合の方式（YMM4・AviUtl と同じレイヤー）で置くときの決まり

テンプレートの棚（:func:`~sashimono.compat.catalog.place`）と ``.exo`` の読み込み
（:func:`~sashimono.compat.aviutl.mapping.map_exo`）が同じ答えを出すよう、ここに置く
片方にだけ書くと、同じ AviUtl のエイリアスが読み込み方によって別のレイヤーへ入る

**レイヤー番号はそのまま混合トラックの並びの番号にする** YMM4 も AviUtl も、番号の
小さいレイヤーが奥で、番号が大きいレイヤーほど手前に描く（YMM4 は 0 始まりなので
写す側で 1 を足してある） こちらの混合トラックもレイヤー 1 が一番奥
（:class:`~sashimono.core.model.Timeline`）なので、向きを裏返さずに並べられる
"""

from __future__ import annotations

from collections.abc import Collection, Iterable

from sashimono.compat.aviutl.report import CompatibilityReport
from sashimono.core.commands import Command
from sashimono.core.commands.layers import new_layer
from sashimono.core.model import MediaItem, Project, Track, TrackKind

__all__ = ["heard_stream", "layer_tracks"]


def layer_tracks(
    project: Project,
    layers: Iterable[int],
    commands: list[Command],
    *,
    heard_only: Collection[int] = (),
) -> dict[int, Track]:
    """元のレイヤー番号 → 置く混合トラック 足りない分は足すコマンドを ``commands`` へ積む

    レイヤー n は、今ある混合トラックの n 本目 間が空いていても埋める 詰めると、
    間を空けて重ねた物の前後が入れ替わる（分ける方式の映像トラックと同じ決まり）

    ``heard_only`` は音だけを置くレイヤー 新しく足すときのソロを音の側で決める
    （:func:`~sashimono.core.commands.layers.solo_for_new_track`） 絵の側で決めると、
    絵のトラックをソロにしているだけで、足した効果音のレイヤーが鳴らなくなる
    """
    existing = [track for track in project.timeline.tracks if track.kind is TrackKind.MIXED]
    tracks: dict[int, Track] = {}
    for layer in range(1, max(layers, default=0) + 1):
        if layer - 1 < len(existing):
            tracks[layer] = existing[layer - 1]
            continue
        tracks[layer] = new_layer(project, commands, picture=layer not in heard_only)
    return tracks


def heard_stream(media: MediaItem | None, track: int, log: CompatibilityReport) -> int | None:
    """素材の ``track`` 本目の音の、素材の中の番号

    置くクリップの :attr:`~sashimono.core.model.Clip.audio_stream` にする
    音を持たない素材と、素材が見つからないときは ``None``（鳴らさない）

    素材に無い本数を選んでいたら 1 本目を鳴らし、数えて残す 無い番号のまま置くと
    混合トラックは置く時点で断り（:class:`~sashimono.core.commands.AddClip`）、
    同じテンプレートのほかの物まで置けなくなる 本数を選べるのは YMM4 だけ
    """
    if media is None or not media.audio_streams:
        return None
    if 0 <= track < len(media.audio_streams):
        return media.audio_streams[track].index
    log.note_missing("YMM4 の音声トラックの選択（AudioTrackIndex）が素材に無い番号")
    return media.audio_streams[0].index
