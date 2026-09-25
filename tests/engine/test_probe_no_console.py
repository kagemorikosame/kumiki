"""素材を調べるときに別のプログラムを起こさないこと

前は回転を読むために ffprobe を起こしていて、窓を持たない配布版では素材を調べるたび
（読み込み・再生の音と絵のデコーダ・控え・字幕起こし）に黒い窓が一瞬出た
同じ素材を何度も調べ直すので、調べた結果を覚えて開く回数も減らす
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import av
import pytest

from sashimono.engine.decode.audio import AudioDecoder
from sashimono.engine.decode.probe import clear_probe_cache, probe_media
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
