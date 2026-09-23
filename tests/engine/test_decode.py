"""実素材に対する解析とデコード

シークの正しさは「飛んだ結果が、先頭から順に読んだ結果と一致するか」でしか
確かめられない 参照列との厳密比較で押さえる
"""

from __future__ import annotations

import subprocess
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest

from sashimono.core.timebase import FrameRate
from sashimono.engine.decode import AudioDecoder, ProbeError, VideoDecoder, probe_media
from tests.media_fixtures import (
    SampleMedia,
    decode_all_frames,
    libx264_available,
    make_delayed,
    make_rotated,
    make_sample,
)


def _short_picture(directory: Path) -> Path:
    """映像 1 秒・音 2 秒の素材 音の方が長い素材の、映像の終わりの後を見るため"""
    if not libx264_available():
        pytest.skip("ffmpeg に libx264 が無いので実素材のテストを飛ばす")
    path = directory / "short-picture.mp4"
    if not path.exists():
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "testsrc2=size=64x48:rate=30:duration=1",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=2:sample_rate=44100",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                str(path),
            ],
            check=True,
            capture_output=True,
        )  # fmt: skip
    return path


class TestProbe:
    def test_reads_video_and_audio(self, sample_av: SampleMedia) -> None:
        item = probe_media(sample_av.path)
        assert item.has_video
        assert item.has_audio
        assert item.name == "av.mp4"

        video = item.video_streams[0]
        assert (video.width, video.height) == (320, 240)
        assert video.frame_rate == FrameRate(30)
        assert video.codec == "h264"
        assert video.pixel_format == "yuv420p"

        audio = item.audio_streams[0]
        assert audio.sample_rate == 44100
        assert audio.channels == 2
        assert audio.codec == "aac"

    def test_duration_is_exact_rational(self, sample_av: SampleMedia) -> None:
        item = probe_media(sample_av.path)
        assert isinstance(item.duration, Fraction)
        assert item.duration == pytest.approx(2.0, abs=0.1)

    def test_fractional_frame_rate_is_preserved(self, sample_ntsc: SampleMedia) -> None:
        # 29.97 が 2997/100 に化けると 1 時間で 3 フレーム以上ずれる
        item = probe_media(sample_ntsc.path)
        assert item.video_streams[0].frame_rate == FrameRate(30000, 1001)

    def test_the_end_of_the_picture_is_read_apart_from_the_sound(self, media_dir: Path) -> None:
        """音の方が長い素材で、映像の道の終わりをコンテナの長さと分けて取る（#115）

        コンテナの長さだけだと、最後の絵で止める時刻が映像の最後のフレームより後ろになり、
        止めた後もデコーダが毎フレーム終わり付近を読み直す
        """
        item = probe_media(_short_picture(media_dir))
        end = item.video_streams[0].end_time
        assert end is not None
        assert float(end) == pytest.approx(1.0, abs=0.05)
        assert item.duration > end + Fraction(1, 2)

    def test_video_only_media(self, sample_long: SampleMedia) -> None:
        item = probe_media(sample_long.path)
        assert item.has_video
        assert not item.has_audio

    def test_rotation_is_detected(self, media_dir: Path, sample_av: SampleMedia) -> None:
        # スマホの縦撮り素材を想定 無視すると横倒しで表示される
        rotated = make_rotated(media_dir, "rot90.mp4", sample_av.path, 90)
        item = probe_media(rotated)
        stream = item.video_streams[0]
        assert stream.rotation == 270
        # 回転を適用すると表示サイズは縦横が入れ替わる
        assert stream.display_size == (240, 320)

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(ProbeError, match="見つからない"):
            probe_media(tmp_path / "無い.mp4")

    def test_not_media(self, tmp_path: Path) -> None:
        path = tmp_path / "notmedia.mp4"
        path.write_bytes(b"not a media file at all")
        with pytest.raises(ProbeError, match="開けない"):
            probe_media(path)


class TestVideoDecoder:
    def test_reads_the_first_frame(self, sample_av: SampleMedia) -> None:
        with VideoDecoder(sample_av.path) as decoder:
            frame = decoder.frame_at(Fraction(0))
        assert frame is not None
        assert frame.shape == (240, 320, 4)
        assert frame.dtype == np.uint8

    def test_sequential_reads_match_the_reference(self, sample_long: SampleMedia) -> None:
        reference = decode_all_frames(sample_long.path)
        with VideoDecoder(sample_long.path) as decoder:
            for index in range(0, 40):
                seconds = Fraction(index, 30)
                frame = decoder.frame_at(seconds)
                assert frame is not None, f"{index} フレーム目が読めない"
                assert np.array_equal(frame, reference[index][1]), f"{index} フレーム目が不一致"

    def test_seek_backwards_lands_exactly(self, sample_long: SampleMedia) -> None:
        # 前方へ飛んでから戻る シークがキーフレームまで戻ってから前進デコードする経路
        reference = decode_all_frames(sample_long.path)
        with VideoDecoder(sample_long.path) as decoder:
            for index in (100, 5, 77, 0, 43):
                frame = decoder.frame_at(Fraction(index, 30))
                assert frame is not None, f"{index} フレーム目が読めない"
                assert np.array_equal(frame, reference[index][1]), f"{index} フレーム目が不一致"

    def test_frame_is_held_until_the_next_one(self, sample_long: SampleMedia) -> None:
        # フレームの表示は次のフレームが来るまで続く その間はどの時刻でも同じ絵
        with VideoDecoder(sample_long.path) as decoder:
            at_start = decoder.frame_at(Fraction(10, 30))
            midway = decoder.frame_at(Fraction(10, 30) + Fraction(1, 90))
        assert at_start is not None
        assert midway is not None
        assert np.array_equal(at_start, midway)

    def test_a_late_starting_file_shows_its_whole_picture(
        self, sample_av: SampleMedia, tmp_path: Path
    ) -> None:
        # 素材の時刻は PTS そのまま 終わりを頭の時刻抜きの長さで切ると、頭が 5 秒の
        # 2 秒の素材は 2 秒から先（映像のすべて）が何も映らない
        late = make_delayed(tmp_path, "late.mp4", sample_av.path, 5.0)
        with VideoDecoder(late) as decoder:
            assert decoder.frame_at(Fraction(6)) is not None
            assert decoder.frame_at(Fraction(7) - Fraction(1, 1000)) is not None
            assert decoder.frame_at(Fraction(8)) is None

    def test_a_late_file_shows_no_picture_where_only_its_sound_goes_on(
        self, media_dir: Path, tmp_path: Path
    ) -> None:
        """頭 5 秒・映像 1 秒・音 2 秒の素材で、映像の終わり（6 秒）の後は絵を出さない

        コンテナの終わり（7 秒）まで出すと、音だけの区間に直前の絵が静止画で残る
        最後の絵を出し続けたいクリップは ``hold_at`` で止める（Issue #115）
        """
        late = make_delayed(tmp_path, "late-short.mp4", _short_picture(media_dir), 5.0)
        with VideoDecoder(late) as decoder:
            assert decoder.frame_at(Fraction(11, 2)) is not None
            assert decoder.frame_at(Fraction(6) - Fraction(1, 1000)) is not None
            assert decoder.frame_at(Fraction(13, 2)) is None

    def test_a_file_whose_sound_runs_longer_shows_no_picture_after_its_picture_ends(
        self, media_dir: Path
    ) -> None:
        # 頭が 0 の素材も同じ 音だけの区間に最後の絵を出すと、映像トラックに置いた動画が
        # 映像の終わりの後も止まった絵のまま、下の層を隠し続ける
        with VideoDecoder(_short_picture(media_dir)) as decoder:
            assert decoder.frame_at(Fraction(1) - Fraction(1, 1000)) is not None
            assert decoder.frame_at(Fraction(3, 2)) is None

    def test_past_the_end_returns_none(self, sample_av: SampleMedia) -> None:
        with VideoDecoder(sample_av.path) as decoder:
            assert decoder.frame_at(Fraction(10)) is None

    def test_negative_time_clamps_to_the_start(self, sample_av: SampleMedia) -> None:
        with VideoDecoder(sample_av.path) as decoder:
            first = decoder.frame_at(Fraction(0))
            before = decoder.frame_at(Fraction(-5))
        assert first is not None
        assert before is not None
        assert np.array_equal(first, before)

    def test_rotation_is_applied(self, media_dir: Path, sample_av: SampleMedia) -> None:
        rotated = make_rotated(media_dir, "rot90b.mp4", sample_av.path, 90)
        with VideoDecoder(rotated) as decoder:
            frame = decoder.frame_at(Fraction(0))
        assert frame is not None
        # 320x240 が縦向きになる 回転を無視していれば (240, 320, 4) のまま
        assert frame.shape == (320, 240, 4)

    def test_media_without_video(self, media_dir: Path) -> None:
        audio_only = make_sample(
            media_dir, "audio_only.m4a", duration=1.0, audio=True, pattern="testsrc2"
        )
        # 映像を含まないファイルを作るため、音声だけ抜き出したものを使う
        import subprocess

        stripped = media_dir / "stripped.m4a"
        if not stripped.exists():
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(audio_only.path),
                    "-vn",
                    "-c:a",
                    "copy",
                    str(stripped),
                ],
                check=True,
                capture_output=True,
            )
        with pytest.raises(ProbeError, match="映像ストリームが無い"):
            VideoDecoder(stripped)


class TestAudioDecoder:
    def test_reads_requested_length(self, sample_av: SampleMedia) -> None:
        with AudioDecoder(sample_av.path, sample_rate=48000, channels=2) as decoder:
            samples = decoder.read(0, 4800)
        assert samples.shape == (4800, 2)
        assert samples.dtype == np.float32

    def test_resamples_to_the_requested_rate(self, sample_av: SampleMedia) -> None:
        # 素材は 44100Hz プロジェクトが 48000Hz ならここで揃える
        with AudioDecoder(sample_av.path, sample_rate=48000) as decoder:
            assert decoder.info.sample_rate == 44100
            assert decoder.sample_rate == 48000
            one_second = decoder.read(0, 48000)
        assert one_second.shape == (48000, 2)
        # lavfi の sine は振幅が小さいので、無音でないことだけを確かめる
        assert np.abs(one_second).max() > 0.01

    def test_reads_are_contiguous(self, sample_av: SampleMedia) -> None:
        # 分けて読んでも、続けて読んだのと同じ波形になること
        with AudioDecoder(sample_av.path, sample_rate=48000) as decoder:
            whole = decoder.read(0, 24000)
        with AudioDecoder(sample_av.path, sample_rate=48000) as decoder:
            first = decoder.read(0, 12000)
            second = decoder.read(12000, 12000)
        assert np.allclose(whole, np.concatenate([first, second]), atol=1e-6)

    def test_seek_returns_the_same_audio(self, sample_av: SampleMedia) -> None:
        # 頭から読んだ 1 秒地点と、飛んで読んだ 1 秒地点が同じ音であること
        #
        # 完全一致はしない シーク時にリサンプラを作り直すため、リサンプルの位相が
        # 1 サンプル未満ずれる 44100Hz から 48000Hz への変換ではサンプル境界が
        # 一致しないので、これは避けられない 可聴域の話ではないので、
        # 波形として同じかを相対 RMS 誤差で見る
        with AudioDecoder(sample_av.path, sample_rate=48000) as decoder:
            sequential = decoder.read(0, 72000)[48000:]
        with AudioDecoder(sample_av.path, sample_rate=48000) as decoder:
            decoder.read(0, 480)
            seeked = decoder.read(48000, 24000)

        signal = float(np.sqrt(np.mean(sequential**2)))
        error = float(np.sqrt(np.mean((sequential - seeked) ** 2)))
        assert error / signal < 0.05, f"相対 RMS 誤差 {error / signal:.3%}"

    def test_before_the_start_is_silent(self, sample_av: SampleMedia) -> None:
        with AudioDecoder(sample_av.path, sample_rate=48000) as decoder:
            samples = decoder.read(-1000, 2000)
        assert np.all(samples[:1000] == 0.0)
        assert np.abs(samples[1000:]).max() > 0.0

    def test_past_the_end_is_silent(self, sample_av: SampleMedia) -> None:
        # 短い配列を返すと呼び出し側が毎回長さを揃える羽目になる 必ず要求長で返す
        with AudioDecoder(sample_av.path, sample_rate=48000) as decoder:
            samples = decoder.read(48000 * 10, 4800)
        assert samples.shape == (4800, 2)
        assert np.all(samples == 0.0)

    def test_mono_downmix(self, sample_av: SampleMedia) -> None:
        with AudioDecoder(sample_av.path, sample_rate=48000, channels=1) as decoder:
            samples = decoder.read(0, 4800)
        assert samples.shape == (4800, 1)

    def test_read_seconds(self, sample_av: SampleMedia) -> None:
        with AudioDecoder(sample_av.path, sample_rate=48000) as decoder:
            samples = decoder.read_seconds(Fraction(1, 2), Fraction(1, 4))
        assert samples.shape == (12000, 2)

    def test_rejects_bad_arguments(self, sample_av: SampleMedia) -> None:
        with pytest.raises(ValueError, match="サンプリングレート"):
            AudioDecoder(sample_av.path, sample_rate=0)
        with pytest.raises(ValueError, match="チャンネル数"):
            AudioDecoder(sample_av.path, sample_rate=48000, channels=3)

    def test_media_without_audio(self, sample_long: SampleMedia) -> None:
        with pytest.raises(ProbeError, match="音声ストリームが無い"):
            AudioDecoder(sample_long.path, sample_rate=48000)
