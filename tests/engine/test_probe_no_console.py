"""素材を調べるときに別のプログラムを起こさないこと

前は回転を読むために ffprobe を起こしていて、窓を持たない配布版では素材を調べるたび
（読み込み・再生の音と絵のデコーダ・控え・字幕起こし）に黒い窓が一瞬出た
同じ素材を何度も調べ直すので、調べた結果を覚えて開く回数も減らす
"""

from __future__ import annotations

import os
import subprocess
import wave
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import av
import pytest

from sashimono.engine.decode import probe as probe_module
from sashimono.engine.decode.audio import AudioDecoder
from sashimono.engine.decode.probe import ROTATION_PACKET_LIMIT, clear_probe_cache, probe_media
from sashimono.engine.decode.video import VideoDecoder
from tests.media_fixtures import SampleMedia, make_rotated


class _NoProcesses:
    """``subprocess.Popen`` の代わり 起こされたら何を起こそうとしたかを覚えて断る"""

    def __init__(self) -> None:
        self.started: list[object] = []

    def __call__(self, args: object, *_rest: object, **_options: object) -> subprocess.Popen[str]:
        self.started.append(args)
        raise AssertionError(f"別のプログラムを起こした: {args}")


def _forbid_processes(monkeypatch: pytest.MonkeyPatch) -> _NoProcesses:
    """ここから先で別のプログラムを起こしたら落とす 素材は先に作っておくこと"""
    guard = _NoProcesses()
    monkeypatch.setattr(subprocess, "Popen", guard)
    return guard


@pytest.fixture(autouse=True)
def fresh_cache() -> None:
    # ほかの試験で調べた結果が残っていると、開かずに答えて何も確かめられない
    clear_probe_cache()


@pytest.mark.parametrize(("degrees", "clockwise"), [(90, 270), (180, 180), (270, 90), (0, 0)])
def test_rotation_is_read_without_starting_a_program(
    media_dir: Path,
    sample_av: SampleMedia,
    monkeypatch: pytest.MonkeyPatch,
    degrees: int,
    clockwise: int,
) -> None:
    # ffprobe を起こすと、窓を持たない配布版で黒い窓が一瞬出る 起こせない所では
    # 回転を 0 と読み、縦撮りが横倒しになる
    rotated = make_rotated(media_dir, f"no_console_rot{degrees}.mp4", sample_av.path, degrees)
    no_processes = _forbid_processes(monkeypatch)
    item = probe_media(rotated)
    (stream,) = item.video_streams
    assert stream.rotation == clockwise
    assert no_processes.started == []


def test_the_decoders_start_no_program(
    sample_av: SampleMedia, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 再生のたびに作るデコーダが素材を調べる 起こすと再生を始めるたびに窓が出る
    no_processes = _forbid_processes(monkeypatch)
    with (
        VideoDecoder(sample_av.path) as video,
        AudioDecoder(sample_av.path, sample_rate=48000) as audio,
    ):
        assert video.info.width == sample_av.width
        assert audio.info.sample_rate == sample_av.sample_rate
    assert no_processes.started == []


def _count_opens(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    opened: list[str] = []
    real = av.open

    def counting(target: str, *args: object, **kwargs: object) -> object:
        opened.append(target)
        return real(target, *args, **kwargs)  # type: ignore[call-overload]  # 数えるだけの素通し

    # 調べる所は av.open を名前で引くので、av の側を差し替えれば数えられる
    monkeypatch.setattr(av, "open", counting)
    return opened


def test_the_same_file_is_opened_once(
    sample_av: SampleMedia, monkeypatch: pytest.MonkeyPatch
) -> None:
    # デコーダを作るたびに開き直すと、再生やシークのたびに素材を開いて頭の 1 枚を復号する
    opened = _count_opens(monkeypatch)
    first = probe_media(sample_av.path)
    second = probe_media(sample_av.path)
    assert len(opened) == 1
    assert first.video_streams == second.video_streams
    assert first.duration == second.duration
    # 素材 ID は呼ぶたびに作る 同じ ID を返すと、2 度読み込んだ素材が 1 つの素材になる
    assert first.id != second.id


def test_a_rewritten_file_is_opened_again(
    tmp_path: Path, sample_av: SampleMedia, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 同じ場所へ書き出し直した素材を覚えた長さのまま置くと、後ろが切れるか空が残る
    copy = tmp_path / "書き直す.mp4"
    copy.write_bytes(sample_av.path.read_bytes())
    opened = _count_opens(monkeypatch)
    probe_media(copy)
    stat = copy.stat()
    os.utime(copy, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    probe_media(copy)
    assert len(opened) == 2


def _wav(path: Path, *, channels: int, rate: int) -> None:
    """1 秒ぶんの無音の wav 声の数と標本の速さを掛けた大きさが同じなら、ファイルも同じ大きさ"""
    with wave.open(str(path), "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(b"\0" * (2 * channels * rate))


def test_a_same_size_overwrite_with_the_old_time_is_read_again(tmp_path: Path) -> None:
    # 時刻を保つ写し方で同じ大きさの別の素材に差し替えると、更新時刻と大きさだけの鍵では
    # 前の素材の声の数や長さのまま置いてしまう（#227 の Qodo の指摘）
    path = tmp_path / "差し替え.wav"
    _wav(path, channels=1, rate=16000)
    before = path.stat()
    assert probe_media(path).audio_streams[0].channels == 1
    _wav(path, channels=2, rate=8000)
    assert path.stat().st_size == before.st_size
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert probe_media(path).audio_streams[0].channels == 2


class _SilentPacket:
    def decode(self) -> list[object]:
        return []


class _NoPictures:
    """絵を 1 枚も出さない映像の入れ物 読んだパケットの数を数える"""

    def __init__(self, packets: int) -> None:
        self.packets = packets
        self.read = 0

    def _packets(self) -> Iterator[_SilentPacket]:
        for _ in range(self.packets):
            self.read += 1
            yield _SilentPacket()

    def demux(self, *_streams: object) -> Iterator[_SilentPacket]:
        return self._packets()

    def decode(self, *_streams: object) -> Iterator[object]:
        for packet in self._packets():
            yield from packet.decode()


def test_waiting_for_the_first_picture_is_bounded() -> None:
    # 絵の出ない映像で頭の 1 枚を待ち続けると、ファイルの終わりまで読み、大きな素材では
    # 読み込みや再生の始まりが止まる（#227 の CodeRabbit の指摘）
    container = _NoPictures(ROTATION_PACKET_LIMIT * 10)
    rotation = probe_module._display_rotation(
        cast(av.container.InputContainer, container), cast(av.VideoStream, object())
    )
    assert rotation == 0
    assert container.read <= ROTATION_PACKET_LIMIT + 1
