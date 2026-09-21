"""タイムラインの音声をミックスする

映像と違い、音声は「今のフレーム」だけでは足りない 再生は連続したサンプル列を
要求するので、フレーム境界をまたぐ範囲をまとめて返せる形にしてある
"""

from __future__ import annotations

from collections import OrderedDict

import numpy as np

from kumiki.core.model import (
    AnimatedValue,
    Clip,
    MediaId,
    ParamValue,
    Project,
    Timeline,
    Track,
    TrackKind,
)
from kumiki.core.timebase import FrameRate
from kumiki.effects.audio import AudioContext
from kumiki.effects.definition import registry
from kumiki.engine.decode import AudioDecoder, ProbeError

__all__ = ["AudioMixer"]

#: 同時に開いておくデコーダの上限
MAX_OPEN_DECODERS = 8

#: シーンの入れ子の深さの上限（映像のレンダラと同じ値）
MAX_SCENE_DEPTH = 8


class AudioMixer:
    """プロジェクトの音声を、指定したサンプル範囲について合成する

    スレッドセーフではない 再生用と書き出し用で別インスタンスにすること
    """

    def __init__(self, project: Project) -> None:
        self._project = project
        self._decoders: OrderedDict[tuple[MediaId, int], AudioDecoder] = OrderedDict()
        self._closed = False

    @property
    def project(self) -> Project:
        return self._project

    @property
    def sample_rate(self) -> int:
        return self._project.settings.sample_rate

    @property
    def channels(self) -> int:
        return self._project.settings.channels

    def set_project(self, project: Project) -> None:
        previous = self._project
        self._project = project
        changed_format = (
            project.settings.sample_rate != previous.settings.sample_rate
            or project.settings.channels != previous.settings.channels
        )
        alive = {m.id for m in project.media}
        for key in [k for k in self._decoders if changed_format or k[0] not in alive]:
            self._decoders.pop(key).close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for decoder in self._decoders.values():
            decoder.close()
        self._decoders.clear()

    def render(self, start_sample: int, count: int) -> np.ndarray:
        """``[start_sample, start_sample + count)`` のミックス結果を返す

        形は ``(count, チャンネル数)`` の float32 クリッピングはしない
        ここで頭打ちにすると、後段のフェードやラウドネス調整で潰れた音しか
        扱えなくなる 出力段で 1 度だけ行う
        """
        if self._closed:
            raise RuntimeError("閉じたミキサは使えない")
        if count <= 0:
            return np.zeros((0, self.channels), dtype=np.float32)

        return self._render_timeline(self._project.timeline, start_sample, count, depth=0)

    def _render_timeline(
        self, timeline: Timeline, start_sample: int, count: int, *, depth: int
    ) -> np.ndarray:
        """1 本のタイムラインの音 入れ子のシーンも同じ道を通る"""
        out = np.zeros((count, self.channels), dtype=np.float32)
        rate = self._project.rate
        for track in timeline.active_tracks(TrackKind.AUDIO):
            self._mix_track(out, track, start_sample, count, rate, depth)
        # シーンを置いたクリップは映像トラックにいる 音も一緒に鳴らさないと、
        # シーンの中の BGM やナレーションが消える
        for track in timeline.active_tracks(TrackKind.VIDEO):
            self._mix_track(out, track, start_sample, count, rate, depth, scenes_only=True)
        return out

    def render_frames(self, start_frame: int, frame_count: int) -> np.ndarray:
        """フレーム範囲で指定してミックスする 書き出し側の入口"""
        rate = self._project.rate
        start = _frame_to_sample(start_frame, rate, self.sample_rate)
        end = _frame_to_sample(start_frame + frame_count, rate, self.sample_rate)
        return self.render(start, end - start)

    def _mix_track(
        self,
        out: np.ndarray,
        track: Track,
        start_sample: int,
        count: int,
        rate: FrameRate,
        depth: int = 0,
        *,
        scenes_only: bool = False,
    ) -> None:
        # 映像トラックの音量と定位は使わない決まり（画面にも出ていない） 置いたシーンの
        # 音だけを混ぜるときに掛けると、見えない値で音が変わる
        gain = 1.0 if scenes_only else _db_to_gain(track.volume_db)
        pan = 0.0 if scenes_only else np.clip(track.pan, -1.0, 1.0)

        for clip in track.clips:
            if not clip.enabled:
                continue
            if scenes_only and clip.scene_id is None:
                continue
            clip_start = _frame_to_sample(clip.timeline_start, rate, self.sample_rate)
            clip_end = _frame_to_sample(clip.timeline_end, rate, self.sample_rate)
            begin = max(start_sample, clip_start)
            end = min(start_sample + count, clip_end)
            if begin >= end:
                continue

            samples = self._read_clip(clip, begin - clip_start, end - begin, rate, depth)
            if samples is None:
                continue
            samples = _apply_effects(
                clip, samples, begin - clip_start, self.sample_rate, clip_end - clip_start, rate
            )

            offset = begin - start_sample
            out[offset : offset + len(samples)] += _apply_pan(samples * gain, float(pan))

    def _read_clip(
        self, clip: Clip, offset_samples: int, count: int, rate: FrameRate, depth: int = 0
    ) -> np.ndarray | None:
        """クリップ内の位置からサンプルを読む 速度変更があればここで反映する"""
        if clip.scene_id is not None:
            return self._read_scene(clip, offset_samples, count, depth)
        if clip.media_id is None:
            return None
        media = self._project.find_media(clip.media_id)
        if media is None or not media.has_audio:
            return None

        decoder = self._decoder_for(clip.media_id, clip.stream_index)
        if decoder is None:
            return None

        source_offset = clip.source_in * self.sample_rate
        if clip.speed == 1:
            start = int(source_offset) + offset_samples
            return decoder.read(start, count)

        # 速度変更 テープを速く回すのと同じで音程も変わる ピッチを保つ
        # タイムストレッチは別物なので、後のフェーズで独立した機能として入れる
        speed = float(clip.speed)
        start = int(source_offset + offset_samples * speed)
        needed = int(np.ceil(count * speed)) + 2
        source = decoder.read(start, needed)
        return _resample_linear(source, count, speed)

    def _read_scene(
        self, clip: Clip, offset_samples: int, count: int, depth: int
    ) -> np.ndarray | None:
        """入れ子のシーンの音 時刻の決まりは映像と同じ（``source_in`` と速度）"""
        scene = self._project.find_scene(clip.scene_id) if clip.scene_id else None
        if scene is None or depth >= MAX_SCENE_DEPTH:
            return None
        source_offset = int(clip.source_in * self.sample_rate)
        if clip.speed == 1:
            return self._render_timeline(
                scene.timeline, source_offset + offset_samples, count, depth=depth + 1
            )
        speed = float(clip.speed)
        start = int(source_offset + offset_samples * speed)
        needed = int(np.ceil(count * speed)) + 2
        source = self._render_timeline(scene.timeline, start, needed, depth=depth + 1)
        return _resample_linear(source, count, speed)

    def _decoder_for(self, media_id: MediaId, stream_index: int) -> AudioDecoder | None:
        key = (media_id, stream_index)
        existing = self._decoders.get(key)
        if existing is not None:
            self._decoders.move_to_end(key)
            return existing

        media = self._project.find_media(media_id)
        if media is None:
            return None
        try:
            decoder = AudioDecoder(
                media.path,
                sample_rate=self.sample_rate,
                channels=self.channels,
                stream_index=stream_index if stream_index else None,
            )
        except ProbeError:
            # オフライン素材 そのクリップだけ無音になり、再生自体は続く
            return None

        self._decoders[key] = decoder
        while len(self._decoders) > MAX_OPEN_DECODERS:
            _, evicted = self._decoders.popitem(last=False)
            evicted.close()
        return decoder


def _apply_effects(
    clip: Clip,
    samples: np.ndarray,
    offset: int,
    sample_rate: int,
    duration: int,
    rate: FrameRate,
) -> np.ndarray:
    """クリップに積んだ音のエフェクトを、置いた順に掛ける

    映像のエフェクトは飛ばす 同じクリップに映像と音の両方が積まれていても、
    音の側だけを見る（AviUtl も音声オブジェクトに映像フィルタを積める）

    値は**この塊の先頭のフレーム**で解く 塊の中で動く値は、フェードのように
    位置で効き方が変わるものが自前で刻む
    """
    frame = int(offset / max(sample_rate, 1) * float(rate.fps))
    for effect in clip.effects:
        if not effect.enabled:
            continue
        definition = registry.get(effect.kind)
        if definition is None or definition.audio_process is None:
            continue
        values = {
            spec.name: _as_number(effect.params.get(spec.name, spec.default_value()), frame)
            for spec in definition.parameters
        }
        samples = definition.audio_process(
            samples, values, AudioContext(offset=offset, sample_rate=sample_rate, duration=duration)
        )
    return samples


def _as_number(value: ParamValue, frame: int) -> float:
    """設定の値を数として読む 読めなければ 0"""
    if isinstance(value, AnimatedValue):
        return float(value.at(frame))
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, int | float):
        return float(value)
    return 0.0


def _frame_to_sample(frame: int, rate: FrameRate, sample_rate: int) -> int:
    """フレーム番号を、そのフレームが始まるサンプル番号へ

    :mod:`kumiki.core.timebase` の変換をそのまま使うと ``Fraction`` の生成が
    サンプルごとに走る ここは再生のたびに通るので、整数演算で済ませる
    """
    return frame * rate.den * sample_rate // rate.num


def _db_to_gain(db: float) -> float:
    if db == 0.0:
        return 1.0
    return float(10.0 ** (db / 20.0))


def _apply_pan(samples: np.ndarray, pan: float) -> np.ndarray:
    """定電力パンニング

    左右の音量を単純な線形で振ると、中央で音圧が下がって聞こえる
    左右のゲインの二乗和が一定になるようにする
    """
    if pan == 0.0 or samples.shape[1] != 2:
        return samples
    angle = (pan + 1.0) * np.pi / 4.0
    gains = np.array([np.cos(angle), np.sin(angle)], dtype=np.float32) * np.float32(np.sqrt(2.0))
    return np.asarray(samples * gains, dtype=np.float32)


def _resample_linear(source: np.ndarray, count: int, speed: float) -> np.ndarray:
    """線形補間でサンプル数を変える

    速度変更のプレビュー品質としては十分 書き出し品質を上げたくなったら、
    ここを多相フィルタに差し替える
    """
    if count <= 0:
        return np.zeros((0, source.shape[1]), dtype=np.float32)

    positions = np.arange(count, dtype=np.float64) * speed
    left = np.floor(positions).astype(np.int64)
    right = np.minimum(left + 1, len(source) - 1)
    left = np.clip(left, 0, max(len(source) - 1, 0))
    weight = (positions - left).astype(np.float32)[:, None]
    if len(source) == 0:
        return np.zeros((count, 1), dtype=np.float32)
    blended = source[left] * (1.0 - weight) + source[right] * weight
    return np.asarray(blended, dtype=np.float32)
