"""YMM4 と比べる道具（tools/ymm4_compare.py）の、フレームとサンプルの数え方

比べる道具がずれていると、描き方が合っていても差が出て、合っていない所を
探し回ることになる 音の側も同じで、枠の切り出しが 1 サンプルずれると
隣の条件の音を測る

音を測る所は、その場で合成した波形で確かめる ffmpeg も YMM4 も要らない
"""

from __future__ import annotations

import importlib.util
import itertools
import json
import sys
from fractions import Fraction
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING

import numpy as np
import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "ymm4_compare", ROOT / "tools" / "ymm4_compare.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_without_the_start_time_ymm4_would_lag_one_frame_behind(tool: ModuleType) -> None:
    """YMM4 の書き出しは最初の 1 枚の時刻が 1 フレーム後ろから始まる

    時刻をそのまま番号にすると YMM4 の絵が 1 枚ずつ遅れて並び、場面の切れ目で
    差が 8〜24 跳ねた（ガタッて落ちるは 23.7、頭の時刻を引くと 0.15）
    """
    base = Fraction(1, 30000)
    assert tool.frame_index(1000, 1000, base, 30.0) == 0
    assert tool.frame_index(2000, 1000, base, 30.0) == 1
    # 頭の時刻が分からない動画は、時刻をそのまま使う
    assert tool.frame_index(3000, None, base, 30.0) == 3


def test_keeping_every_frame_would_run_out_of_memory_so_they_stream(tool: ModuleType) -> None:
    # 1 万枚を超える書き出しを全部持つとメモリに載らない 若い順に進めて読み、
    # 比べる 1 枚だけを配列へ変換する（読み飛ばす絵まで変換すると数分かかる）
    converted: list[int] = []

    def picture(index: int) -> Callable[[], np.ndarray]:
        def convert() -> np.ndarray:
            converted.append(index)
            return np.full((1, 1, 3), index, dtype=np.uint8)

        return convert

    references = tool.References((index, picture(index)) for index in (0, 1, 2, 4))
    first = references.get(1)
    assert first is not None and int(first[0, 0, 0]) == 1
    # 動画に無い番号は None
    assert references.get(3) is None
    last = references.get(4)
    assert last is not None and int(last[0, 0, 0]) == 4
    assert references.get(9) is None
    assert converted == [1, 4]


def test_asking_backwards_fails_instead_of_silently_skipping_a_comparison(tool: ModuleType) -> None:
    # 読み進めた動画は戻せない 黙って None を返すと、並べ間違いが
    # 「比べる絵が無い」に化けて、比べた枚数が減ったことに気付けない
    references = tool.References(iter([(5, lambda: np.zeros((1, 1, 3), dtype=np.uint8))]))
    assert references.get(5) is not None
    with pytest.raises(ValueError, match="若い順"):
        references.get(2)


def test_three_samples_miss_a_one_frame_glitch_so_every_frame_can_be_compared(
    tool: ModuleType,
) -> None:
    # 3 枚だけだと、切れ目のように一瞬だけずれる所を見落とす
    case = tool.Case(name="n", file="f", index=0, start=100, length=60)
    assert case.sample_frames() == [102, 129, 156]
    assert case.sample_frames(every=True) == list(range(100, 160))


def tone(
    rate: int, seconds: float, *, left: float = 1.0, right: float = 1.0, hz: float = 440.0
) -> np.ndarray:
    """左右で振幅の違う正弦波 測る所だけを確かめるので ffmpeg も YMM4 も要らない"""
    time = np.arange(int(rate * seconds), dtype=np.float32) / rate
    wave = np.sin(2.0 * np.pi * hz * time, dtype=np.float32)
    return np.stack([wave * left, wave * right])


def test_a_one_sample_slip_in_the_slot_would_measure_the_neighbour_slot(tool: ModuleType) -> None:
    """枠の切り出しが 1 サンプルずれると、隣の枠の音を測ってしまう

    フレームからサンプルへ直すときに切り捨てると、枠の頭が前の枠へ食い込み、
    前の枠が鳴っているあいだは「無音のはずの枠が鳴っている」と読める
    """
    rate, fps = 48000, 30
    # 枠は 120 フレーム（4 秒）ごと 隣り合う枠の端がぴったり接する
    assert tool.slot_bounds(0, 120, fps, rate) == (0, 192000)
    assert tool.slot_bounds(120, 120, fps, rate) == (192000, 384000)
    # 割り切れないフレーム数でも、前の枠の終わりと次の枠の始まりが同じ番号になる
    first_end = tool.slot_bounds(0, 7, fps, 44100)[1]
    assert first_end == tool.slot_bounds(7, 7, fps, 44100)[0]

    samples = np.zeros((2, 384000), dtype=np.float32)
    loud = tone(rate, 4.0)
    samples[:, :192000] = loud
    begin, end = tool.slot_bounds(120, 120, fps, rate)
    quiet = tool.measure_block(samples[:, begin:end], rate)
    assert quiet.seconds == 0.0
    # 1 サンプル手前から切ると、前の枠の音が混ざって無音でなくなる
    slipped = tool.measure_block(samples[:, begin - 1 : end - 1], rate)
    assert slipped.peak[0] > 0.0


def test_rms_and_peak_and_sounding_length_are_read_per_channel(tool: ModuleType) -> None:
    # 定位の向きは左右の比でしか読めない 左右をまとめて測ると、どちらへ寄ったのか消える
    rate = 48000
    block = np.zeros((2, rate * 4), dtype=np.float32)
    block[:, : rate * 2] = tone(rate, 2.0, left=1.0, right=0.25)
    measured = tool.measure_block(block, rate)
    assert measured.peak[0] == pytest.approx(1.0, abs=1e-3)
    assert measured.peak[1] == pytest.approx(0.25, abs=1e-3)
    # 正弦波の RMS は振幅の 1/√2 半分は無音なので、さらに 1/√2
    assert measured.rms[0] == pytest.approx(0.5, abs=1e-3)
    assert measured.rms[1] == pytest.approx(0.125, abs=1e-3)
    # 鳴っている長さは、枠の長さ（4 秒）ではなく鳴った 2 秒
    assert measured.seconds == pytest.approx(2.0, abs=0.02)
    assert measured.hz == pytest.approx(440.0, abs=1.0)


def test_a_silent_slot_reports_zero_length_so_playback_rate_zero_can_be_told_apart(
    tool: ModuleType,
) -> None:
    # PlaybackRate が 0 のとき、止まるのか等倍なのかは長さでしか分からない
    rate = 48000
    silent = tool.measure_block(np.zeros((2, rate * 4), dtype=np.float32), rate)
    assert silent.seconds == 0.0
    assert silent.hz == 0.0
    # 基準が無音でも、比を出すところで 0 では割らない
    assert tool.ratio(0.5, 0.0) == 0.0
    assert tool.ratio(0.25, 0.5) == pytest.approx(0.5)


def test_the_volume_guesses_separate_a_plain_share_from_a_decibel_dial(tool: ModuleType) -> None:
    # 50 のときに振幅比なら 0.5、dB 目盛りなら桁違いに小さい どちらに近いかで読み分ける
    guesses = tool.volume_guesses(50.0)
    assert guesses["振幅比"] == pytest.approx(0.5)
    assert guesses["二乗"] == pytest.approx(0.25)
    assert guesses["dB目盛り"] == pytest.approx(10.0 ** (-30.0 / 20.0))
    assert tool.volume_guesses(100.0)["dB目盛り"] == pytest.approx(1.0)


def test_the_probe_slots_never_overlap_and_match_the_manifest(tool: ModuleType) -> None:
    # 枠が重なると、1 つの条件の音に隣の条件が混ざって、どちらの値も読めない
    slots = tool.build_audio_slots()
    manifest = tool.audio_manifest(slots, Path("tone.wav"))
    assert len(manifest["slots"]) == len(slots)
    assert [entry["name"] for entry in manifest["slots"]] == [slot.name for slot in slots]
    starts = [slot.start for slot in slots]
    assert starts == sorted(starts)
    for before, after in itertools.pairwise(starts):
        assert after - before >= tool.AUDIO_SLOT + tool.AUDIO_GAP
    # 基準の枠は既定の値だけを持つ ここがずれると、すべての比の元がずれる
    base = slots[manifest["baseline"]]
    assert (base.volume, base.pan, base.playback_rate) == (100.0, 0.0, 100.0)
    # 測りたい条件がすべて並んでいる
    assert {slot.volume for slot in slots if slot.kind == "volume"} == {0.0, 10, 25, 50, 75, 90}
    assert {slot.pan for slot in slots if slot.kind == "pan"} == {-100.0, -50.0, 50.0, 100.0}
    assert {slot.playback_rate for slot in slots if slot.kind == "rate"} == {0.0, 50.0, 200.0}


def test_the_probe_project_is_written_the_way_ymm4_writes_audio_items(tool: ModuleType) -> None:
    # 形を推測すると YMM4 がプロジェクトを開けない 実物の AudioItem の項目に合わせる
    slots = tool.build_audio_slots()
    item = tool.audio_item(slots[1], Path("tone.wav"))
    assert item["$type"] == "YukkuriMovieMaker.Project.Items.AudioItem, YukkuriMovieMaker"
    # Volume と Pan は動く値、PlaybackRate はただの数 実物 125 個がこの食い違いを持つ
    assert item["Volume"]["Values"] == [{"Value": slots[1].volume}]
    assert item["Pan"]["Values"] == [{"Value": slots[1].pan}]
    assert isinstance(item["PlaybackRate"], float)
    assert item["Frame"] == slots[1].start
    assert item["Length"] == tool.AUDIO_SLOT


def test_the_project_keeps_the_bom_that_ymm4_needs(tool: ModuleType, tmp_path: Path) -> None:
    # BOM が無いと YMM4 はプロジェクトを開けない 絵の比較と同じ書き方を使う
    slots = tool.build_audio_slots()
    target = tmp_path / "audio-probe.ymmp"
    items = [tool.audio_item(slot, tmp_path / "tone.wav") for slot in slots]
    tool.write_document(items, 1000, target)
    raw = target.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    document = json.loads(raw.decode("utf-8-sig"))
    timeline = document["Timelines"][0]
    assert timeline["VideoInfo"]["Hz"] == tool.AUDIO_RATE
    assert len(timeline["Items"]) == len(slots)


def test_measuring_before_the_export_explains_itself_instead_of_crashing(
    tool: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # 書き出しは手作業 まだ無いときに例外で落ちると、手順を間違えたのか
    # 道具が壊れたのか分からない
    arguments = SimpleNamespace(work=tmp_path)
    assert tool.command_audio_measure(arguments) == 0
    assert "audio-build" in capsys.readouterr().out
    slots = tool.build_audio_slots()
    (tmp_path / "audio-probe.json").write_text(
        json.dumps(tool.audio_manifest(slots, tmp_path / "tone.wav")), encoding="utf-8"
    )
    assert tool.command_audio_measure(arguments) == 0
    assert "まだ書き出されていません" in capsys.readouterr().out
