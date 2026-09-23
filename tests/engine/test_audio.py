"""波形解析とミックス

音のずれは編集の終盤まで気づきにくいので、位置とレベルを数値で押さえる
"""

from __future__ import annotations

from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest

from sashimono.core.commands import AddClip, AddMedia, AddTrack
from sashimono.core.model import Clip, Project, ProjectSettings, Track, TrackKind
from sashimono.core.timebase import FrameRate
from sashimono.engine.audio import AudioMixer, analyze_waveform
from sashimono.engine.audio.waveform import BASE_SAMPLES_PER_PEAK
from sashimono.engine.decode import probe_media
from tests.media_fixtures import SampleMedia, make_silent_gap


@pytest.fixture(scope="session")
def gapped_audio(media_dir: Path) -> Path:
    """前後に音、真ん中が無音の 6 秒の素材"""
    return make_silent_gap(media_dir, "gapped.wav", duration=6.0)


class TestWaveform:
    def test_builds_multiple_levels(self, sample_av: SampleMedia) -> None:
        # 1 段階だけだと、拡大時は粗く縮小時は読む量が多すぎる
        waveform = analyze_waveform(sample_av.path)
        assert waveform is not None
        assert len(waveform.levels) >= 2
        assert waveform.levels[0].samples_per_peak == BASE_SAMPLES_PER_PEAK
        for finer, coarser in zip(waveform.levels, waveform.levels[1:], strict=False):
            assert coarser.samples_per_peak > finer.samples_per_peak
            assert coarser.count < finer.count

    def test_covers_the_whole_media(self, sample_av: SampleMedia) -> None:
        waveform = analyze_waveform(sample_av.path)
        assert waveform is not None
        assert float(waveform.duration) == pytest.approx(2.0, abs=0.1)

    def test_peaks_bracket_the_signal(self, sample_av: SampleMedia) -> None:
        # min は必ず max 以下 逆転していれば描画が破綻する
        waveform = analyze_waveform(sample_av.path)
        assert waveform is not None
        for level in waveform.levels:
            assert np.all(level.peaks[:, :, 0] <= level.peaks[:, :, 1])

    def test_level_matches_the_zoom(self, sample_av: SampleMedia) -> None:
        waveform = analyze_waveform(sample_av.path)
        assert waveform is not None
        # 極端に拡大したら最も細かい段階
        assert waveform.level_for(1).samples_per_peak == BASE_SAMPLES_PER_PEAK
        # 極端に縮小したら最も粗い段階
        assert waveform.level_for(10**9).samples_per_peak == waveform.levels[-1].samples_per_peak

    def test_envelope_shape(self, sample_av: SampleMedia) -> None:
        waveform = analyze_waveform(sample_av.path)
        assert waveform is not None
        envelope = waveform.envelope(0, waveform.total_samples, 200)
        assert envelope.shape == (200, 2, 2)
        assert np.all(envelope[:, :, 0] <= envelope[:, :, 1])

    def test_envelope_finds_the_silence(self, gapped_audio: Path) -> None:
        # 真ん中が無音の素材 エンベロープの真ん中だけ振幅が落ちること
        waveform = analyze_waveform(gapped_audio)
        assert waveform is not None
        envelope = waveform.envelope(0, waveform.total_samples, 30)
        amplitude = np.abs(envelope).max(axis=(1, 2))

        assert amplitude[2] > 0.01, "前半に音が無い"
        assert amplitude[15] < 1e-4, "中央が無音になっていない"
        assert amplitude[27] > 0.01, "後半に音が無い"

    def test_envelope_handles_degenerate_input(self, sample_av: SampleMedia) -> None:
        waveform = analyze_waveform(sample_av.path)
        assert waveform is not None
        assert waveform.envelope(0, 0, 10).shape == (10, 2, 2)
        assert waveform.envelope(0, 1000, 0).shape == (0, 2, 2)

    def test_can_be_cancelled(self, sample_av: SampleMedia) -> None:
        # 素材を差し替えたのに前の解析が走り続ける、という状態を避ける
        assert analyze_waveform(sample_av.path, should_cancel=lambda: True) is None

    def test_reports_progress(self, sample_av: SampleMedia) -> None:
        seen: list[float] = []
        analyze_waveform(sample_av.path, progress=seen.append)
        assert seen
        assert seen[-1] == 1.0
        assert all(0.0 <= value <= 1.0 for value in seen)
        assert seen == sorted(seen)


@pytest.fixture
def audio_project(sample_av: SampleMedia) -> Project:
    """2 秒の素材を音声トラックへ 1 本置いたプロジェクト"""
    media = probe_media(sample_av.path)
    project = Project.create(
        ProjectSettings(width=320, height=240, frame_rate=FrameRate(30), sample_rate=48000)
    )
    project = AddMedia(media).apply(project)
    track = Track(kind=TrackKind.AUDIO, name="A1")
    project = AddTrack(track).apply(project)
    clip = Clip(timeline_start=0, duration=60, media_id=media.id)
    return AddClip(track.id, clip).apply(project)


def rms(samples: np.ndarray) -> float:
    return float(np.sqrt(np.mean(samples**2)))


class TestMixer:
    def test_renders_requested_length(self, audio_project: Project) -> None:
        mixer = AudioMixer(audio_project)
        try:
            block = mixer.render(0, 4800)
        finally:
            mixer.close()
        assert block.shape == (4800, 2)
        assert block.dtype == np.float32
        assert rms(block) > 0.0

    def test_silence_outside_the_clip(self, audio_project: Project) -> None:
        # クリップは 60 フレーム = 2 秒 その後ろは無音
        mixer = AudioMixer(audio_project)
        try:
            beyond = mixer.render(48000 * 5, 4800)
        finally:
            mixer.close()
        assert np.all(beyond == 0.0)

    def test_clip_position_shifts_the_audio(self, audio_project: Project) -> None:
        # クリップを 1 秒後ろへ動かすと、0 秒地点は無音、1 秒地点に音が来る
        track = audio_project.timeline.tracks[0]
        moved = track.clips[0].moved_to(30)
        project = audio_project.with_timeline(
            audio_project.timeline.replace_track(track.with_clips((moved,)))
        )
        mixer = AudioMixer(project)
        try:
            before = mixer.render(0, 24000)
            after = mixer.render(48000, 24000)
        finally:
            mixer.close()
        assert np.all(before == 0.0)
        assert rms(after) > 0.0

    def test_track_volume(self, audio_project: Project) -> None:
        mixer = AudioMixer(audio_project)
        try:
            full = mixer.render(0, 24000)
        finally:
            mixer.close()

        track = audio_project.timeline.tracks[0]
        quieter = audio_project.with_timeline(
            audio_project.timeline.replace_track(replace(track, volume_db=-6.0))
        )
        mixer = AudioMixer(quieter)
        try:
            reduced = mixer.render(0, 24000)
        finally:
            mixer.close()

        # -6dB はおよそ半分
        assert rms(reduced) / rms(full) == pytest.approx(0.5, abs=0.02)

    def test_muted_track_is_silent(self, audio_project: Project) -> None:
        track = audio_project.timeline.tracks[0]
        project = audio_project.with_timeline(
            audio_project.timeline.replace_track(replace(track, muted=True))
        )
        mixer = AudioMixer(project)
        try:
            assert np.all(mixer.render(0, 4800) == 0.0)
        finally:
            mixer.close()

    def test_solo_silences_the_others(self, audio_project: Project) -> None:
        # もう 1 本トラックを足し、片方だけソロにする
        media = audio_project.media[0]
        second = Track(kind=TrackKind.AUDIO, name="A2", solo=True)
        project = AddTrack(second).apply(audio_project)
        project = AddClip(second.id, Clip(timeline_start=0, duration=60, media_id=media.id)).apply(
            project
        )

        mixer = AudioMixer(project)
        try:
            soloed = mixer.render(0, 24000)
        finally:
            mixer.close()

        mixer = AudioMixer(audio_project)
        try:
            single = mixer.render(0, 24000)
        finally:
            mixer.close()

        # ソロにした 1 本だけが鳴るので、元の 1 本と同じレベルになる
        assert rms(soloed) == pytest.approx(rms(single), rel=0.01)

    def test_two_tracks_add_up(self, audio_project: Project) -> None:
        media = audio_project.media[0]
        second = Track(kind=TrackKind.AUDIO, name="A2")
        project = AddTrack(second).apply(audio_project)
        project = AddClip(second.id, Clip(timeline_start=0, duration=60, media_id=media.id)).apply(
            project
        )

        mixer = AudioMixer(project)
        try:
            doubled = mixer.render(0, 24000)
        finally:
            mixer.close()

        mixer = AudioMixer(audio_project)
        try:
            single = mixer.render(0, 24000)
        finally:
            mixer.close()

        assert rms(doubled) == pytest.approx(rms(single) * 2, rel=0.05)

    def test_does_not_clip_internally(self, audio_project: Project) -> None:
        # 内部で頭打ちにすると、後段のフェードやラウドネス調整で潰れた音しか
        # 扱えなくなる 合成段では 1.0 を超えたままにしておく
        track = audio_project.timeline.tracks[0]
        loud = audio_project.with_timeline(
            audio_project.timeline.replace_track(replace(track, volume_db=40.0))
        )
        mixer = AudioMixer(loud)
        try:
            block = mixer.render(0, 24000)
        finally:
            mixer.close()
        assert np.abs(block).max() > 1.0

    def test_pan_keeps_power_constant(self, audio_project: Project) -> None:
        # 単純な線形パンだと中央で音圧が下がる 定電力なら左右に振っても
        # 全体のパワーが変わらない
        track = audio_project.timeline.tracks[0]
        mixer = AudioMixer(audio_project)
        try:
            centred = mixer.render(0, 24000)
        finally:
            mixer.close()

        panned_project = audio_project.with_timeline(
            audio_project.timeline.replace_track(replace(track, pan=1.0))
        )
        mixer = AudioMixer(panned_project)
        try:
            panned = mixer.render(0, 24000)
        finally:
            mixer.close()

        assert rms(panned) == pytest.approx(rms(centred), rel=0.02)
        # 完全に右へ振ったので左は無音
        assert rms(panned[:, 0]) < rms(panned[:, 1]) * 0.01

    def test_speed_shortens_the_source_consumed(self, audio_project: Project) -> None:
        # 2 倍速のクリップの 0.5 秒地点は、等倍の 1 秒地点と同じ音
        track = audio_project.timeline.tracks[0]
        fast = replace(track.clips[0], speed=Fraction(2), duration=30)
        project = audio_project.with_timeline(
            audio_project.timeline.replace_track(track.with_clips((fast,)))
        )

        mixer = AudioMixer(project)
        try:
            sped = mixer.render(24000, 2400)
        finally:
            mixer.close()

        mixer = AudioMixer(audio_project)
        try:
            normal = mixer.render(48000, 4800)
        finally:
            mixer.close()

        # 2 倍速側の 1 サンプルが等倍側の 2 サンプルに対応する
        assert rms(sped) == pytest.approx(rms(normal), rel=0.15)

    def test_a_held_clip_keeps_its_sound_moving(self, audio_project: Project) -> None:
        """絵を止めた（Issue #115）クリップでも、音は止めずに素材を読み進める

        音まで止めると、止めた時刻のサンプルを伸ばした音（うなり）が鳴る YMM4 でも
        素材の終わりの後は無音で、再生速度 0 は鳴らない（音量 0 で写している）
        """
        track = audio_project.timeline.tracks[0]
        held = replace(track.clips[0], hold_at=Fraction(0))
        project = audio_project.with_timeline(
            audio_project.timeline.replace_track(track.with_clips((held,)))
        )

        mixer = AudioMixer(project)
        try:
            sounded = mixer.render(24000, 4800)
        finally:
            mixer.close()
        mixer = AudioMixer(audio_project)
        try:
            normal = mixer.render(24000, 4800)
        finally:
            mixer.close()

        assert np.array_equal(sounded, normal)

    def test_a_held_clip_is_silent_past_the_end_of_its_source(self, audio_project: Project) -> None:
        # 素材 2 秒を 3 秒の枠へ 素材の終わりの後に最後のサンプルを伸ばして鳴らすと、
        # YMM4 に無い音が出る
        track = audio_project.timeline.tracks[0]
        media = audio_project.media[0]
        held = replace(track.clips[0], duration=90, hold_at=media.duration - Fraction(1, 30))
        project = audio_project.with_timeline(
            audio_project.timeline.replace_track(track.with_clips((held,)))
        )

        mixer = AudioMixer(project)
        try:
            # 2.5 秒目から 0.25 秒
            tail = mixer.render(120000, 12000)
        finally:
            mixer.close()

        assert rms(tail) == 0.0

    def test_render_frames(self, audio_project: Project) -> None:
        mixer = AudioMixer(audio_project)
        try:
            block = mixer.render_frames(0, 30)
        finally:
            mixer.close()
        # 30fps の 30 フレーム = 1 秒 = 48000 サンプル
        assert block.shape == (48000, 2)

    def test_video_only_media_contributes_nothing(
        self, sample_long: SampleMedia, audio_project: Project
    ) -> None:
        media = probe_media(sample_long.path)
        project = AddMedia(media).apply(audio_project)
        track = project.timeline.tracks[0]
        project = project.with_timeline(
            project.timeline.replace_track(
                track.with_clips((Clip(timeline_start=0, duration=60, media_id=media.id),))
            )
        )
        mixer = AudioMixer(project)
        try:
            assert np.all(mixer.render(0, 4800) == 0.0)
        finally:
            mixer.close()

    def test_offline_media_is_silent_instead_of_crashing(self, audio_project: Project) -> None:
        original = audio_project.media[0]
        broken = replace(original, path=original.path.parent / "行方不明.wav")
        project = audio_project.replace_media(broken)
        mixer = AudioMixer(project)
        try:
            assert np.all(mixer.render(0, 4800) == 0.0)
        finally:
            mixer.close()

    def test_closed_mixer_refuses(self, audio_project: Project) -> None:
        mixer = AudioMixer(audio_project)
        mixer.close()
        with pytest.raises(RuntimeError, match="閉じたミキサ"):
            mixer.render(0, 100)
