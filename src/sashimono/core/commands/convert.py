"""置き方の方式を途中で切り替えたときに、置いてあるトラックも変換する（Issue #27）

分ける方式（映像トラックと音声トラック）と混合の方式（レイヤー）の行き来を 1 つの命令
（:class:`ConvertLayers`）で行い、1 回の取り消しで戻す 変換の前後で書き出す絵と音は
変えない 変えられない所（ミュートとソロの効き方など）は :func:`convert_layers` が
知らせる文言として返し、画面は変換の前にそれを見せる

**分ける → 混合** 映像トラックを並びのまま 1 本ずつレイヤーにする 音声トラックの
クリップは、リンクの相手（同じ素材・位置・長さ・素材の中の位置・速さ）が映像トラックに
あれば相手へまとめ、無ければ（BGM・片方だけトリムした物）音声トラックごとに作る
絵を描かないレイヤーへ移す

**混合 → 分ける** レイヤーを 1 本ずつ映像トラックと音声トラックに分ける 絵も音も
出すクリップは 2 本に分けて新しいリンクで結ぶ 映像トラックはレイヤーのあった所に、
音声トラックはそのすぐ後ろに置く 素材を置いたときに足すトラックと同じく映像と音声が
組で並ぶので、置いた作品は行きと帰りで同じ並び（V1 A1 V2 A2）に戻る 分ける方式の
画面は種類ごとにまとめて並べ、音声トラックの並びは聞こえ方に関わらないので、
V1 V2 A1 A2 と並んでいた作品が V1 A1 V2 A2 に戻っても絵と音は変わらない

どちらの向きも、重なり順（:attr:`Timeline.tracks` の並び）は絵を描くトラックの間で
変えない 映像トラックとレイヤーは同じ並びの意味（先頭が一番奥）を持つので、並びを
そのまま写せば重なりも変わらない（:class:`~sashimono.core.model.Timeline`）
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field, replace

from sashimono.core.commands.base import Command
from sashimono.core.commands.edit import SetLayerMode
from sashimono.core.commands.fixed import VOLUME_EFFECT_KIND, fixed_slot, loose_slot
from sashimono.core.model import (
    AnimatedValue,
    Clip,
    ClipId,
    Effect,
    GroupId,
    LayerMode,
    Project,
    Timeline,
    Track,
    TrackKind,
    default_track_name,
)
from sashimono.core.model.ids import new_clip_id, new_group_id, new_track_id

__all__ = ["ConvertLayers", "LayerConversion", "convert_layers", "switch_layer_mode"]

#: 音量調整の ``音量`` の上限（%） 定義（:mod:`sashimono.effects.audio`）の範囲と同じ
#: 越えた値は読むときに縮められるので、写しきれないことを知らせる
_VOLUME_LIMIT = 400.0

#: 本人が名前を付けていないトラック（作ったときの名前のまま） 変換したら行き先の種類の
#: 名前へ付け直す 付け直さないと、レイヤーに「V1」、音声トラックに「レイヤー 2」が残る
_DEFAULT_NAME = re.compile(r"(?:V|A|レイヤー )\d+")


@dataclass(frozen=True, slots=True)
class LayerConversion:
    """変換した結果と、変換で引き継げない物の一覧

    ``notices`` は画面に並べる文言 空なら、書き出す絵と音は変換の前後で同じ
    """

    project: Project
    notices: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ConvertLayers(Command):
    """置いてあるトラックを ``layer_mode`` の方式へ変換する（メインとすべてのシーン）

    方式の設定（:attr:`ProjectSettings.layer_mode`）は変えない 設定だけを変えたいことも
    あるので、:class:`~sashimono.core.commands.SetLayerMode` と組にして使う
    （:func:`switch_layer_mode`） 1 つにまとめると、設定だけ戻したいときにも変換まで戻る

    ``sound_kinds`` は音を加工するエフェクトの種類（``registry.sound_kinds()``）
    コア層はエフェクトの定義を読めないので、呼び手が渡す 絵と音をまとめる・分けるときに、
    エフェクトをどちらの側へ持たせるかをこれで決める

    変換するトラックが無ければ何も変えない（履歴に段を積まない）
    """

    layer_mode: str
    sound_kinds: frozenset[str] = field(default_factory=frozenset)

    @property
    def label(self) -> str:
        name = "混合" if self.layer_mode == LayerMode.MIXED else "映像と音声に分ける"
        return f"トラックを変換: {name}"

    def apply(self, project: Project) -> Project:
        return convert_layers(project, self.layer_mode, self.sound_kinds).project


def switch_layer_mode(
    project: Project, layer_mode: str, *, convert: bool, sound_kinds: frozenset[str]
) -> list[Command]:
    """方式を ``layer_mode`` へ切り替える命令 ``convert`` が真なら置いてあるトラックも変換する

    画面は 2 つを 1 回の取り消しで戻せるようにまとめて実行する
    """
    commands: list[Command] = []
    if project.settings.layer_mode != layer_mode:
        commands.append(SetLayerMode(layer_mode))
    if convert:
        commands.append(ConvertLayers(layer_mode, sound_kinds))
    return commands


def convert_layers(
    project: Project, layer_mode: str, sound_kinds: frozenset[str]
) -> LayerConversion:
    """``project`` のトラックを ``layer_mode`` へ変換した結果と、引き継げない物の一覧

    画面が変換の前に一覧だけを見るときもこれを呼ぶ 一覧を別に求めると、実際の変換と
    食い違った一覧を見せることになる
    """
    if layer_mode not in LayerMode.ALL:
        raise ValueError(f"トラックの方式が不正: {layer_mode!r}")
    # 同じ文言（音声トラックの音量を写しきれない など）はクリップごとに出るので、1 つにまとめる
    notices: list[str] = []
    timeline = _convert_timeline(project, project.timeline, layer_mode, sound_kinds, "", notices)
    scenes = tuple(
        replace(
            scene,
            timeline=_convert_timeline(
                project,
                scene.timeline,
                layer_mode,
                sound_kinds,
                f"シーン「{scene.name}」の ",
                notices,
            ),
        )
        for scene in project.scenes
    )
    unique = tuple(dict.fromkeys(notices))
    converted = replace(project, timeline=timeline, scenes=scenes)
    if converted.timeline == project.timeline and converted.scenes == project.scenes:
        # 変えていないのに別のオブジェクトを返すと、何もしない段が履歴に積まれる
        return LayerConversion(project, unique)
    return LayerConversion(converted, unique)


# --- 1 本のタイムライン ---


@dataclass(slots=True)
class _Carry:
    """元のクリップの絵と音を、変換後のどのクリップが受け持つか

    変換の前後で「映っていたのに隠れた」「鳴っていたのに止まった」を見つけるために持つ
    """

    picture: dict[ClipId, list[ClipId]] = field(default_factory=dict)
    sound: dict[ClipId, list[ClipId]] = field(default_factory=dict)

    def both(self, origin: Clip, carrier: Clip) -> None:
        self.picture.setdefault(origin.id, []).append(carrier.id)
        self.sound.setdefault(origin.id, []).append(carrier.id)


def _convert_timeline(
    project: Project,
    timeline: Timeline,
    layer_mode: str,
    sound_kinds: frozenset[str],
    where: str,
    notices: list[str],
) -> Timeline:
    carry = _Carry()
    if layer_mode == LayerMode.MIXED:
        tracks = _to_mixed(project, timeline, sound_kinds, carry, where, notices)
    else:
        tracks = _to_separated(project, timeline, sound_kinds, carry, where, notices)
    if tracks is None:
        return timeline
    converted = replace(timeline, tracks=_without_lone_links(tracks))
    notices.extend(_state_changes(project, timeline, converted, carry, where))
    return converted


def _to_mixed(
    project: Project,
    timeline: Timeline,
    sound_kinds: frozenset[str],
    carry: _Carry,
    where: str,
    notices: list[str],
) -> tuple[Track, ...] | None:
    """映像トラックをレイヤーへ、音声トラックの音をリンクの相手へまとめる

    変える物（レイヤーでないトラック）が無ければ ``None``
    """
    if not any(t.kind is not TrackKind.MIXED for t in timeline.tracks):
        return None
    partners = _pair_sounds(timeline)
    paired = {sound.id for _, sound in partners.values()}
    stereo = project.settings.channels == 2
    tracks: list[Track] = []
    renamed: set[int] = set()
    for track in timeline.tracks:
        if track.kind is TrackKind.MIXED:
            tracks.append(track)
            continue
        if track.kind is TrackKind.VIDEO:
            clips: list[Clip] = []
            dropped = 0
            for clip in track.clips:
                pair = partners.get(clip.id)
                if pair is None:
                    # 相手の無い絵は鳴らさない 音の番号を残したままだと、映像トラックで
                    # 鳴っていなかった素材の音がレイヤーで鳴りだす
                    merged = replace(clip, audio_stream=None, show_picture=True)
                    carry.both(clip, merged)
                else:
                    sound_track, sound = pair
                    gain = _gain_effect(sound_track.volume_db, sound_track.pan, stereo=stereo)
                    if gain is not None and gain[1]:
                        notices.append(
                            f"{where}{sound_track.name} の音量 {sound_track.volume_db:+.1f} dB は、"
                            f"クリップの音量調整の上限（{_VOLUME_LIMIT:.0f}%）までしか写せない"
                        )
                    merged, lost = _merged(clip, sound, sound_kinds, gain[0] if gain else None)
                    dropped += lost
                    # まとめた 1 本の音は音声のクリップの物 映像のクリップの音（鳴っていなかった）
                    # の受け手に数えると、変換で音が鳴りだしたと知らせてしまう
                    carry.picture.setdefault(clip.id, []).append(merged.id)
                    carry.sound.setdefault(sound.id, []).append(merged.id)
                clips.append(merged)
            if dropped:
                notices.append(
                    f"{where}{track.name} の {dropped} 個のエフェクトは外す"
                    "（映像トラックの音・音声トラックの絵に付いていて効いていなかった物）"
                )
            if _DEFAULT_NAME.fullmatch(track.name) or not track.name:
                renamed.add(len(tracks))
            # 映像トラックの音量と定位は使わない決まり（シーンの音も掛けずに混ぜる）
            # レイヤーは掛けるので、残すと置いたシーンの音が変わる
            tracks.append(
                replace(track, kind=TrackKind.MIXED, clips=tuple(clips), volume_db=0.0, pan=0.0)
            )
            continue
        # 音声トラック 相手へまとめた残り（BGM・片方だけトリムした物）を、絵を描かない
        # レイヤーへ移す トラックの音量・定位・ミュート・ソロはレイヤーがそのまま持てる
        rest = []
        for clip in track.clips:
            if clip.id in paired:
                continue
            moved = replace(
                clip,
                show_picture=False,
                audio_stream=clip.stream_index if clip.media_id is not None else None,
            )
            carry.both(clip, moved)
            rest.append(moved)
        if not rest:
            if track.effects:
                notices.append(
                    f"{where}{track.name} はクリップがすべて映像へまとまるので消える"
                    " トラックに付けたエフェクトは引き継がない"
                )
            continue
        if _DEFAULT_NAME.fullmatch(track.name) or not track.name:
            renamed.add(len(tracks))
        tracks.append(replace(track, kind=TrackKind.MIXED, clips=tuple(rest)))
    return _named(tracks, renamed)


def _pair_sounds(timeline: Timeline) -> dict[ClipId, tuple[Track, Clip]]:
    """映像トラックのクリップ ID → まとめる音声のクリップ（とそのトラック）

    リンクの相手でも、まとめて 1 本にすると聞こえ方か見え方が変わる物は組にしない
    （位置・長さ・素材の中の位置・速さ・有効かどうか・束ね・トラックのミュートとソロと
    ロックが違う物） 片方だけトリムした物もここで外れ、音だけのレイヤーへ移る
    多言語の音のように 1 つの絵に音が 2 本ある物は、先に並ぶ音声トラックの 1 本とだけ組む
    """
    pictures: dict[GroupId, list[tuple[Track, Clip]]] = {}
    for track in timeline.tracks:
        if track.kind is not TrackKind.VIDEO:
            continue
        for clip in track.clips:
            if clip.link_group is not None and clip.media_id is not None:
                pictures.setdefault(clip.link_group, []).append((track, clip))
    pairs: dict[ClipId, tuple[Track, Clip]] = {}
    for track in timeline.tracks:
        if track.kind is not TrackKind.AUDIO:
            continue
        for sound in track.clips:
            if sound.link_group is None or sound.media_id is None:
                continue
            for picture_track, picture in pictures.get(sound.link_group, []):
                if picture.id not in pairs and _pairs_with(picture_track, picture, track, sound):
                    pairs[picture.id] = (track, sound)
                    break
    return pairs


def _pairs_with(picture_track: Track, picture: Clip, sound_track: Track, sound: Clip) -> bool:
    same_clip = (
        picture.media_id == sound.media_id
        and picture.timeline_start == sound.timeline_start
        and picture.duration == sound.duration
        and picture.source_in == sound.source_in
        and picture.speed == sound.speed
        and picture.enabled == sound.enabled
        and picture.group_id == sound.group_id
    )
    # トラックの状態が違うとまとめられない まとめたレイヤーは 1 つのミュートとソロで
    # 絵と音の両方を決めるので、音声トラックだけミュートしていた音が鳴りだす
    same_track = (
        picture_track.muted == sound_track.muted
        and picture_track.solo == sound_track.solo
        and picture_track.locked == sound_track.locked
    )
    return same_clip and same_track


def _merged(
    picture: Clip, sound: Clip, sound_kinds: frozenset[str], gain: Effect | None
) -> tuple[Clip, int]:
    """絵のクリップへ音のクリップをまとめた 1 本と、外したエフェクトの数

    エフェクトは、絵の側から音のエフェクトを、音の側から絵のエフェクトを外して並べる
    どちらも元のトラックでは効いていなかった物で、まとめると効きだす
    （映像のクリップに残っていた音量調整で、まとめた音が小さくなる）

    音のエフェクトは、固定の項目は :func:`fixed_slot`、ふつうの物は :func:`loose_slot` の
    位置へ入れる ただし音の側の中の並びは崩さない（前に入れた音のエフェクトより
    前へは入れない） 左右を混ぜるモノラル化と定位の順が入れ替わると音が変わる

    ``gain`` は音声トラックの音量と定位を写した音量調整 トラックの音量はクリップの
    エフェクトをすべて掛けた後に掛かるので、音のふつうのエフェクトより後ろに置く
    後ろに並ぶのが固定の音量とフェードだけなら、どちらも左右それぞれへ掛ける倍率で、
    掛ける順を入れ替えても音は変わらない
    """
    effects = [e for e in picture.effects if e.kind not in sound_kinds]
    lost = len(picture.effects) - len(effects)
    cursor = 0
    for effect in sound.effects:
        if effect.kind not in sound_kinds:
            lost += 1
            continue
        slot = fixed_slot(effects, effect.kind) if effect.fixed else loose_slot(effects)
        slot = max(slot, cursor)
        effects.insert(slot, effect)
        cursor = slot + 1
    if gain is not None:
        _insert_gain(effects, gain, sound_kinds)
    merged = replace(
        picture,
        effects=tuple(effects),
        audio_stream=sound.stream_index,
        show_picture=True,
    )
    return merged, lost


def _insert_gain(effects: list[Effect], gain: Effect, sound_kinds: frozenset[str]) -> None:
    """トラックの音量を写した音量調整を、音のふつうのエフェクトの後ろ（固定の項目の前）へ"""
    after_loose = max(
        (i + 1 for i, e in enumerate(effects) if e.kind in sound_kinds and not e.fixed), default=0
    )
    effects.insert(max(loose_slot(effects), after_loose), gain)


def _gain_effect(volume_db: float, pan: float, *, stereo: bool) -> tuple[Effect, bool] | None:
    """トラックの音量と定位を写した音量調整（固定ではない）と、上限で縮めたか 変えないなら ``None``

    トラックの定位は左右の二乗和を保つ振り方（:func:`sashimono.engine.audio.mixer._apply_pan`）
    音量調整の左右は片側を絞るだけ 同じ定位の値を渡すと音が変わるので、左右それぞれの
    倍率が同じになる音量と左右を求める 大きい側を音量に、小さい側を絞る量にする
    """
    gain = 10.0 ** (volume_db / 20.0)
    left = right = gain
    if stereo and pan != 0.0:
        angle = (max(-1.0, min(1.0, pan)) + 1.0) * math.pi / 4.0
        left = gain * math.sqrt(2.0) * math.cos(angle)
        right = gain * math.sqrt(2.0) * math.sin(angle)
    loud = max(left, right)
    volume = loud * 100.0
    if loud <= 0.0:
        balance = 0.0
    elif right >= left:
        balance = (1.0 - left / loud) * 100.0
    else:
        balance = -(1.0 - right / loud) * 100.0
    if math.isclose(volume, 100.0, abs_tol=1e-9) and math.isclose(balance, 0.0, abs_tol=1e-9):
        return None
    clamped = volume > _VOLUME_LIMIT
    effect = Effect(
        kind=VOLUME_EFFECT_KIND,
        params={
            "volume": AnimatedValue(static=min(volume, _VOLUME_LIMIT)),
            "pan": AnimatedValue(static=balance),
        },
    )
    return effect, clamped


def _to_separated(
    project: Project,
    timeline: Timeline,
    sound_kinds: frozenset[str],
    carry: _Carry,
    where: str,
    notices: list[str],
) -> tuple[Track, ...] | None:
    """レイヤーを映像トラックと音声トラックに分ける 変える物が無ければ ``None``"""
    if not any(t.kind is TrackKind.MIXED for t in timeline.tracks):
        return None
    stereo = project.settings.channels == 2
    tracks: list[Track] = []
    renamed: set[int] = set()
    for track in timeline.tracks:
        if track.kind is not TrackKind.MIXED:
            tracks.append(track)
            continue
        pictures: list[Clip] = []
        sounds: list[Clip] = []
        silent = 0
        for clip in track.clips:
            draws = project.draws_picture(track, clip)
            plays = project.plays_sound(track, clip)
            if clip.scene_id is not None:
                if draws:
                    # 映像トラックのシーンはトラックの音量を掛けずに鳴る レイヤーの音量で
                    # 鳴っていたのを保つため、同じ量をクリップへ写す
                    gain = _gain_effect(track.volume_db, track.pan, stereo=stereo)
                    moved = _picture_part(clip, None)
                    if gain is not None:
                        effects = list(moved.effects)
                        _insert_gain(effects, gain[0], sound_kinds)
                        moved = replace(moved, effects=tuple(effects))
                    pictures.append(moved)
                else:
                    moved = _sound_part(clip, None, None)
                    sounds.append(moved)
                carry.both(clip, moved)
                continue
            if draws and plays:
                link = new_group_id()
                picture = _picture_part(clip, link)
                picture = replace(
                    picture, effects=tuple(e for e in clip.effects if e.kind not in sound_kinds)
                )
                sound = _sound_part(
                    clip, link, tuple(e for e in clip.effects if e.kind in sound_kinds)
                )
                pictures.append(picture)
                sounds.append(sound)
                carry.picture.setdefault(clip.id, []).append(picture.id)
                carry.sound.setdefault(clip.id, []).append(sound.id)
            elif plays:
                moved = _sound_part(clip, None, None)
                sounds.append(moved)
                carry.both(clip, moved)
            else:
                moved = _picture_part(clip, None)
                if not draws:
                    # 絵も音も出していないクリップ（音を選んでいない、絵を隠した動画など）
                    # 映像トラックでは絵が出てしまうので無効にして置く 消すと元に戻せない
                    moved = replace(moved, enabled=False)
                    silent += 1
                pictures.append(moved)
                carry.both(clip, moved)
        if silent:
            notices.append(
                f"{where}{track.name} の {silent} 本は絵も音も出していないので、"
                "無効にして映像トラックへ移す"
            )
        default_name = bool(_DEFAULT_NAME.fullmatch(track.name)) or not track.name
        if pictures or not sounds:
            # 空のレイヤーも映像トラックとして残す 消すと、何も置いていない所に作った
            # 並びが変換の行き帰りで失われる
            if default_name:
                renamed.add(len(tracks))
            tracks.append(
                replace(track, kind=TrackKind.VIDEO, clips=tuple(pictures), volume_db=0.0, pan=0.0)
            )
        if sounds:
            if default_name:
                renamed.add(len(tracks))
            tracks.append(
                replace(
                    track,
                    kind=TrackKind.AUDIO,
                    clips=tuple(sounds),
                    # 絵を持つ側がレイヤーの ID を引き継ぎ、音の側は新しいトラックにする
                    id=new_track_id() if pictures else track.id,
                    effects=() if pictures else track.effects,
                )
            )
    return _named(tracks, renamed)


def _picture_part(clip: Clip, link: GroupId | None) -> Clip:
    """混合トラックのクリップを映像トラックへ置く形 音の番号と絵の表示の印は既定へ戻す"""
    return replace(clip, audio_stream=None, show_picture=True, link_group=link or clip.link_group)


def _sound_part(clip: Clip, link: GroupId | None, effects: tuple[Effect, ...] | None) -> Clip:
    """混合トラックのクリップを音声トラックへ置く形

    絵を描くクリップから分けるときは新しい ID で作り、絵にしか効かない欄（不透明度・
    合成・切り抜き・止める時刻・画素で置く）は既定にする 素材を置いたときに分ける方式が
    作る音のクリップと同じ形にしておくと、もう 1 度混合へ変換したときに相手として
    見つかり、行き帰りで元の 1 本に戻る
    """
    if effects is None:
        # 音だけのクリップはそのまま移す（ID も欄も保つ）
        return replace(
            clip,
            stream_index=clip.audio_stream if clip.audio_stream is not None else clip.stream_index,
            audio_stream=None,
            show_picture=True,
        )
    assert clip.audio_stream is not None
    return Clip(
        timeline_start=clip.timeline_start,
        duration=clip.duration,
        media_id=clip.media_id,
        source_in=clip.source_in,
        stream_index=clip.audio_stream,
        speed=clip.speed,
        effects=effects,
        enabled=clip.enabled,
        link_group=link,
        group_id=clip.group_id,
        id=new_clip_id(),
    )


def _named(tracks: list[Track], renamed: set[int]) -> tuple[Track, ...]:
    """``renamed`` の番号のトラックへ、種類ごとの並びの番号で名前を付け直す"""
    counts: Counter[TrackKind] = Counter()
    result: list[Track] = []
    fixed_names = {t.name for i, t in enumerate(tracks) if i not in renamed}
    for index, track in enumerate(tracks):
        counts[track.kind] += 1
        if index in renamed:
            taken = fixed_names | {t.name for t in result}
            track = replace(track, name=default_track_name(track.kind, counts[track.kind], taken))
        result.append(track)
    return tuple(result)


def _without_lone_links(tracks: Iterable[Track]) -> tuple[Track, ...]:
    """相手の居なくなったリンクを外す まとめた 1 本や、まとめ残した音に古い組が残るため

    1 本だけのリンクを残すと、あとで同じ組の値を持つクリップができたときに誤って連動する
    """
    listed = tuple(tracks)
    counts = Counter(c.link_group for t in listed for c in t.clips if c.link_group is not None)
    lone = {group for group, count in counts.items() if count == 1}
    if not lone:
        return listed
    return tuple(
        replace(
            track,
            clips=tuple(
                replace(c, link_group=None) if c.link_group in lone else c for c in track.clips
            ),
        )
        for track in listed
    )


# --- 変換の前後で見え方・聞こえ方が変わった所 ---


def _visible(project: Project, timeline: Timeline) -> set[ClipId]:
    shown = {t.id for t in timeline.active_picture_tracks()}
    return {
        clip.id
        for track in timeline.tracks
        if track.id in shown
        for clip in track.clips
        if clip.enabled and project.draws_picture(track, clip)
    }


def _audible(project: Project, timeline: Timeline) -> set[ClipId]:
    """鳴るクリップ ミキサ（:mod:`sashimono.engine.audio.mixer`）と同じ決まりで数える

    映像トラックのシーンは絵の側のミュートとソロで鳴る
    """
    heard = {t.id for t in timeline.active_sound_tracks()}
    shown = {t.id for t in timeline.active_picture_tracks()}
    result: set[ClipId] = set()
    for track in timeline.tracks:
        for clip in track.clips:
            if not clip.enabled or not project.plays_sound(track, clip):
                continue
            active = shown if track.kind is TrackKind.VIDEO else heard
            if track.id in active:
                result.add(clip.id)
    return result


def _state_changes(
    project: Project, before: Timeline, after: Timeline, carry: _Carry, where: str
) -> list[str]:
    """変換の前後で、映る・鳴るが変わるクリップを元のトラックごとに数えた文言

    ミュートとソロは、分ける方式では絵（映像トラック）と音（音声トラック）で別々に
    効くが、レイヤーは 1 つで両方を決める 行き先で同じ効き方にできない物がここに出る
    """
    seen_before, heard_before = _visible(project, before), _audible(project, before)
    seen_after, heard_after = _visible(project, after), _audible(project, after)
    changes: Counter[tuple[str, str]] = Counter()
    for track in before.tracks:
        for clip in track.clips:
            shown = clip.id in seen_before
            if any(c in seen_after for c in carry.picture.get(clip.id, ())) != shown:
                changes[(track.name, "絵が隠れる" if shown else "絵が出るようになる")] += 1
            heard = clip.id in heard_before
            if any(c in heard_after for c in carry.sound.get(clip.id, ())) != heard:
                changes[(track.name, "音が止まる" if heard else "音が鳴るようになる")] += 1
    return [
        f"{where}{name} の {count} 本: {change}（ミュート・ソロの効き方が変わるため）"
        for (name, change), count in changes.items()
    ]
