"""固定の項目（:attr:`Effect.fixed`）

クリップが最初から持つ欄（YMM4 の描画・音声の項目にあたる）は、外すことと
並べ替えることを断り、無効にはできる 同じ種類を重ねて掛けたいときは、印の無い
ふつうのエフェクトとして足す（#27 の YMM4 型アイテムの設計 P1）
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import numpy as np
import pytest

from sashimono.compat.catalog import restyle
from sashimono.compat.mapped import MappedObject
from sashimono.core.commands import (
    AddEffect,
    AddTrack,
    MoveEffect,
    ParamPath,
    RemoveEffect,
    SetEffectEnabled,
    SetParam,
    insert_media,
)
from sashimono.core.commands.insert import VOLUME_EFFECT_KIND
from sashimono.core.io import Preset
from sashimono.core.io.aliases import Alias
from sashimono.core.io.serialize import (
    effect_from_json,
    effect_to_json,
    project_from_dict,
    project_to_dict,
)
from sashimono.core.model import (
    AnimatedValue,
    Clip,
    Effect,
    GeneratedSource,
    MediaItem,
    Project,
    Track,
    TrackKind,
)
from sashimono.core.timebase import FrameRate
from sashimono.effects import registry


def _placed(media: MediaItem) -> Project:
    project = Project.create()
    for command in insert_media(project, media):
        project = command.apply(project)
    return project


def _sound(project: Project) -> Clip:
    (clip,) = [c for t in project.timeline.tracks if t.kind is TrackKind.AUDIO for c in t.clips]
    return clip


def _volume(volume: float, *, fixed: bool = False) -> Effect:
    effect = registry.require(VOLUME_EFFECT_KIND).create(volume=volume)
    return replace(effect, fixed=fixed)


def _blur() -> Effect:
    return registry.require("blur").create(radius=4)


def _with_effects(project: Project, *effects: Effect) -> Project:
    clip = _sound(project)
    for effect in effects:
        project = AddEffect(clip.id, effect).apply(project)
    return project


class TestTheMark:
    def test_changing_a_value_keeps_the_mark(self) -> None:
        # 欄を並べて作り直すと、値を 1 つ触っただけで印が落ち、外せる物に戻る
        effect = _volume(100, fixed=True)
        assert effect.with_param("volume", AnimatedValue(50.0)).fixed

    def test_setting_a_param_through_the_command_keeps_the_mark(
        self, audio_media: MediaItem
    ) -> None:
        # 設定パネルの音量のつまみは SetParam を出す ここで印が落ちると、音量を
        # 1 度動かしただけで固定の欄が消せる物になる
        project = _placed(audio_media)
        clip = _sound(project)
        fixed = clip.effects[0]
        project = SetParam(
            ParamPath.of_effect(clip.id, fixed.id, "volume"), AnimatedValue(40.0)
        ).apply(project)
        assert _sound(project).effects[0].fixed

    def test_placed_sound_gets_a_fixed_volume(self, audio_media: MediaItem) -> None:
        # 置いたときに付く音量調整が外せると、YMM4 の音声アイテムと違って音量の欄が消える
        (effect,) = _sound(_placed(audio_media)).effects
        assert effect.kind == VOLUME_EFFECT_KIND
        assert effect.fixed


class TestCommands:
    def test_a_fixed_effect_cannot_be_removed(self, audio_media: MediaItem) -> None:
        project = _placed(audio_media)
        clip = _sound(project)
        with pytest.raises(ValueError, match="外せない"):
            RemoveEffect(clip.id, clip.effects[0].id).apply(project)

    def test_a_normal_effect_can_still_be_removed(self, audio_media: MediaItem) -> None:
        # 固定の物のついでに、ふつうのエフェクトまで外せなくなっていないこと
        project = _with_effects(_placed(audio_media), _blur())
        clip = _sound(project)
        project = RemoveEffect(clip.id, clip.effects[1].id).apply(project)
        assert len(_sound(project).effects) == 1

    def test_a_fixed_effect_can_be_turned_off(self, audio_media: MediaItem) -> None:
        # 外せない代わりに、効かせたくないときは無効にする 無効まで断ると逃げ道が無い
        project = _placed(audio_media)
        clip = _sound(project)
        project = SetEffectEnabled(clip.id, clip.effects[0].id, False).apply(project)
        effect = _sound(project).effects[0]
        assert not effect.enabled and effect.fixed

    def test_a_fixed_effect_cannot_be_moved(self, audio_media: MediaItem) -> None:
        project = _with_effects(_placed(audio_media), _blur())
        clip = _sound(project)
        with pytest.raises(ValueError, match="並べ替えられない"):
            MoveEffect(clip.id, clip.effects[0].id, 1).apply(project)

    def test_a_normal_effect_cannot_jump_over_a_fixed_one(self, audio_media: MediaItem) -> None:
        # 固定の欄の前へ割り込ませると、どこまでが最初からある欄か見分けが付かなくなる
        project = _with_effects(_placed(audio_media), _blur())
        clip = _sound(project)
        with pytest.raises(ValueError, match="またいで"):
            MoveEffect(clip.id, clip.effects[1].id, 0).apply(project)

    def test_normal_effects_still_swap_among_themselves(self, audio_media: MediaItem) -> None:
        # 固定の物の後ろにあるふつうのエフェクトどうしは、今までどおり並べ替えられる
        project = _with_effects(_placed(audio_media), _blur(), _volume(50))
        clip = _sound(project)
        project = MoveEffect(clip.id, clip.effects[2].id, 1).apply(project)
        kinds = [e.kind for e in _sound(project).effects]
        assert kinds == [VOLUME_EFFECT_KIND, VOLUME_EFFECT_KIND, "blur"]

    def test_the_same_kind_can_be_stacked(self, audio_media: MediaItem) -> None:
        # 重ねがけは YMM4 と同じく許す 同じ種類だからと断ると、音量を 2 段で絞れない
        project = _with_effects(_placed(audio_media), _volume(50))
        effects = _sound(project).effects
        assert [e.kind for e in effects] == [VOLUME_EFFECT_KIND, VOLUME_EFFECT_KIND]
        assert [e.fixed for e in effects] == [True, False]
        # 足した方は外せる
        project = RemoveEffect(_sound(project).id, effects[1].id).apply(project)
        assert len(_sound(project).effects) == 1

    def test_stacked_volumes_both_apply(self, audio_media: MediaItem) -> None:
        # 重ねた方が鳴らないと、足したのに何も変わらないように見える 50% を 2 回で 25%
        from sashimono.engine.audio.mixer import _apply_effects

        project = _placed(audio_media)
        clip = _sound(project)
        project = SetParam(
            ParamPath.of_effect(clip.id, clip.effects[0].id, "volume"), AnimatedValue(50.0)
        ).apply(project)
        project = _with_effects(project, _volume(50))
        samples = np.full((64, 2), 0.8, dtype=np.float32)
        out = _apply_effects(_sound(project), samples, 0, 48000, 64, FrameRate(30))
        assert np.allclose(out, 0.2)


class TestFile:
    def test_the_mark_survives_a_round_trip(self) -> None:
        effect = _volume(80, fixed=True)
        assert effect_from_json(effect_to_json(effect)).fixed
        assert not effect_from_json(effect_to_json(_volume(80))).fixed

    def test_the_kind_matches_the_placed_volume(self) -> None:
        # 読み書きの層は命令の層を読まないので同じ値を別に書いている 食い違うと、
        # 前の版のファイルの音量調整が格上げされず、固定の物と 2 つ並ぶ
        from sashimono.core.io.serialize import _PLACED_VOLUME_KIND

        assert _PLACED_VOLUME_KIND == VOLUME_EFFECT_KIND

    @staticmethod
    def _as_old_file(project: Project) -> dict[str, Any]:
        """印を知らない前の本体が書いたのと同じ形にする"""
        data = project_to_dict(project)
        for track in data["timeline"]["tracks"]:
            for clip in track["clips"]:
                for effect in clip["effects"]:
                    effect.pop("fixed", None)
        return data

    def test_the_placed_volume_of_an_old_file_becomes_fixed(self, audio_media: MediaItem) -> None:
        # #145 の本体で置いた音量調整は印を持たない そのまま開くと、後で固定の音量調整が
        # 足されたときに同じ物が 2 つ並び、どちらが最初からある欄か分からなくなる
        # 置いた後で音量を動かしていても、置いたときに付いた物に変わりはない
        project = _placed(audio_media)
        clip = _sound(project)
        project = SetParam(
            ParamPath.of_effect(clip.id, clip.effects[0].id, "volume"), AnimatedValue(70.0)
        ).apply(project)
        project = _with_effects(project, _volume(50))
        loaded = project_from_dict(self._as_old_file(project))
        assert [e.fixed for e in _sound(loaded).effects] == [True, False]

    def test_a_new_file_is_left_as_written(self, audio_media: MediaItem) -> None:
        # 今の本体が書いたファイルでは、印の無い先頭の音量調整は本人が足した物
        # 開くたびに格上げすると、外せたはずの物が外せなくなる
        project = _placed(audio_media)
        clip = _sound(project)
        track = next(t for t in project.timeline.tracks if t.kind is TrackKind.AUDIO)
        unfixed = project.with_timeline(
            project.timeline.replace_track(
                track.with_clips((replace(clip, effects=(_volume(70),)),))
            )
        )
        loaded = project_from_dict(project_to_dict(unfixed))
        assert not _sound(loaded).effects[0].fixed

    def test_text_clips_of_an_old_file_are_left_alone(self) -> None:
        # 素材のクリップでなければ置いたときの音量調整ではない
        clip = Clip(
            timeline_start=0,
            duration=30,
            source=GeneratedSource(kind="text", params={"text": "字"}),
            effects=(_volume(70),),
        )
        project = AddTrack(Track(kind=TrackKind.VIDEO, clips=(clip,))).apply(Project.create())
        loaded = project_from_dict(self._as_old_file(project))
        (text,) = [c for t in loaded.timeline.tracks for c in t.clips]
        assert not text.effects[0].fixed


class TestCopies:
    def test_a_preset_drops_the_mark(self) -> None:
        # 印のまま当てると、プリセットを当てるたびに外せないエフェクトが増えていく
        preset = Preset(name="音", effects=(_volume(50, fixed=True), _blur()))
        assert not any(e.fixed for e in preset.effects)
        assert not any(e.fixed for e in preset.instantiate())
        assert not any(e.fixed for e in Preset.from_dict(preset.to_dict()).effects)

    def test_an_alias_keeps_the_mark(self) -> None:
        # エイリアスはクリップを丸ごと写す 置いたクリップは元と同じ欄を持つ
        clip = Clip(
            timeline_start=0,
            duration=30,
            source=GeneratedSource(kind="text", params={"text": "字"}),
            effects=(_volume(50, fixed=True),),
        )
        placed = Alias(name="字", clip=clip).instantiate()
        assert placed.effects[0].fixed
        assert placed.effects[0].id != clip.effects[0].id

    def test_restyle_leaves_the_fixed_effect(self) -> None:
        # 着せ替えは今のエフェクトを全部外してから足す 固定の物まで外そうとすると
        # 命令が断られ、着せ替え自体ができなくなる
        border = registry.require("border").create()
        template = MappedObject(
            clip=Clip(
                timeline_start=0,
                duration=30,
                source=GeneratedSource(kind="text", params={"text": "見本"}),
                effects=(border,),
            ),
            layer=1,
        )
        fixed, loose = _volume(50, fixed=True), _blur()
        target = Clip(
            timeline_start=0,
            duration=30,
            source=GeneratedSource(kind="text", params={"text": "字"}),
            effects=(fixed, loose),
        )
        commands = restyle([template], target)
        removed = {c.effect_id for c in commands if isinstance(c, RemoveEffect)}
        assert removed == {loose.id}
