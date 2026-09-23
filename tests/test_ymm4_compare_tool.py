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
import os
import sys
from fractions import Fraction
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING

import numpy as np
import pytest

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

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


def _manifest_and_video(
    tool: ModuleType, work: Path, *, newer: int = 60, content: bytes = b""
) -> Path:
    """枠の一覧と、書き出し（既定は中身の無いもの）を置く

    書き出しの時刻は ``newer`` 秒だけ先にする 置き場によっては時刻が 2 秒刻みで
    しか残らず、続けて書くと同じ時刻になって「古い書き出し」と見なされる
    """
    slots = tool.build_audio_slots()
    manifest = work / "audio-probe.json"
    manifest.write_text(json.dumps(tool.audio_manifest(slots, work / "tone.wav")), encoding="utf-8")
    return _export_after(manifest, work / "audio-probe.mp4", newer=newer, content=content)


def _export_after(manifest: Path, video: Path, *, newer: int, content: bytes) -> Path:
    """書き出しを置き、その時刻を枠の一覧より ``newer`` 秒先にする

    音・格子・絵の速さの探りで同じ 時刻を明示しないと、続けて書いた 2 つが同じ時刻に
    なり（置き場によっては 2 秒刻みでしか残らない）「古い書き出し」と見なされる
    """
    video.write_bytes(content)
    stamp = manifest.stat()
    os.utime(video, (stamp.st_atime + newer, stamp.st_mtime + newer))
    return video


def test_an_export_older_than_the_probe_is_refused(
    tool: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """探りを作り直した後に古い書き出しを測ると、別の条件を測った表が出る

    枠の並びが変わっているのに気付けないので、`Pan` の向きを読み違えたまま
    実装を直してしまう
    """
    _manifest_and_video(tool, tmp_path, newer=-60)
    assert tool.command_audio_measure(SimpleNamespace(work=tmp_path)) == 0
    assert "作り直す前の書き出し" in capsys.readouterr().out


def test_an_export_with_the_same_time_as_the_probe_is_refused(
    tool: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """同じ時刻は「あとで書き出した」証しにならない

    置き場によっては時刻が 2 秒刻みでしか残らず、作り直した直後の書き出しと
    一覧が同じ時刻になる そこを通すと、古い音を新しい枠で切り出す
    """
    _manifest_and_video(tool, tmp_path, newer=0)
    assert tool.command_audio_measure(SimpleNamespace(work=tmp_path)) == 0
    assert "作り直す前の書き出し" in capsys.readouterr().out


def test_an_export_whose_sound_is_empty_explains_itself(
    tool: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """音の道はあるが中身が無い書き出しを測ると、全部の枠が 0 秒・比 0 になり
    「再生速度 0 で止まる」と読み違える
    """
    _manifest_and_video(tool, tmp_path)
    # 前に測った表を置いておく 残ったままだと、新しい結果として開けてしまう
    (tmp_path / "audio-report.json").write_text("{}", encoding="utf-8")
    empty = np.zeros((2, 0), dtype=np.float32)
    monkeypatch.setattr(tool, "decode_audio", lambda _video: (empty, tool.AUDIO_RATE))
    assert tool.command_audio_measure(SimpleNamespace(work=tmp_path)) == 0
    assert "音が空です" in capsys.readouterr().out
    assert not (tmp_path / "audio-report.json").exists()


def test_the_start_time_is_read_in_the_stream_unit(tool: ModuleType) -> None:
    """頭の時刻とサンプルの時刻は刻みが違う 同じ刻みとして引くと枠ごとずれる

    ずれた枠は別の条件の音や無音を測るので、音量の曲線を丸ごと読み違える
    """
    # 頭の時刻は 1/90000 刻みで 9000（＝ 0.1 秒）、一切れは 1/48000 刻みで 4800
    at = tool.sample_index(4800, 9000, Fraction(1, 48000), 48000, Fraction(1, 90000))
    assert at == 4800 - 4800
    # 同じ刻みなら今までどおり
    assert tool.sample_index(4800, 480, Fraction(1, 48000), 48000) == 4320


def test_an_export_without_sound_explains_itself(
    tool: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """音の道が無い書き出しで `IndexError` で終わると、測り方の案内が出ない"""
    _manifest_and_video(tool, tmp_path)
    monkeypatch.setattr(tool, "decode_audio", lambda _video: None)
    assert tool.command_audio_measure(SimpleNamespace(work=tmp_path)) == 0
    assert "音の道がありません" in capsys.readouterr().out


def test_the_build_warns_about_a_stale_export(
    tool: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """作り直したことに気付かないまま measure へ進むと、古い音を測る"""
    monkeypatch.setattr(tool, "make_tone", lambda target: target.write_bytes(b"") or True)
    (tmp_path / "audio-probe.mp4").write_bytes(b"")
    (tmp_path / "audio-report.json").write_text("{}", encoding="utf-8")
    assert tool.command_audio_build(SimpleNamespace(work=tmp_path)) == 0
    assert "前の探りの書き出し" in capsys.readouterr().out
    # 前の測り結果が残ると、新しい枠の一覧に対応しない表を読んでしまう
    assert not (tmp_path / "audio-report.json").exists()


def test_the_probe_writes_an_absolute_path_for_the_tone(
    tool: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """相対のまま書くと YMM4 が正弦波を見つけられず、全部の枠が無音になる

    無音を測ると「再生速度 0 で止まる」「音量の比が 0」と読めてしまう
    """
    monkeypatch.setattr(tool, "make_tone", lambda target: target.write_bytes(b"") or True)
    monkeypatch.chdir(tmp_path)
    assert tool.command_audio_build(SimpleNamespace(work=Path("work"))) == 0
    raw = (tmp_path / "work" / "audio-probe.ymmp").read_bytes().decode("utf-8-sig")
    paths = [item["FilePath"] for item in json.loads(raw)["Timelines"][0]["Items"]]
    assert paths and all(Path(path).is_absolute() for path in paths)


def test_a_piece_before_the_head_is_trimmed_not_wrapped(tool: ModuleType) -> None:
    """頭より前の一切れを負の位置のまま置くと、先頭の音が終わりへ書かれる

    AAC の先読み分などで最初の一切れが頭より前に来ることがある 末尾に音が
    混じると、最後の枠の音量を取り違える
    """
    early = np.ones((2, 4), dtype=np.float32)
    later = np.full((2, 4), 0.5, dtype=np.float32)
    joined = tool.assemble_audio([(-2, early), (2, later)])
    assert joined.shape == (2, 6)
    assert joined[0].tolist() == [1.0, 1.0, 0.5, 0.5, 0.5, 0.5]


def test_pieces_entirely_before_the_head_leave_nothing(tool: ModuleType) -> None:
    """全部が頭より前なら空 例外で落ちると、測り方の案内も出せない"""
    early = np.ones((2, 4), dtype=np.float32)
    assert tool.assemble_audio([(-10, early)]).shape == (2, 0)
    assert tool.assemble_audio([]).shape == (2, 0)


# 格子の点の並びの探り（Issue #107） YMM4 も ffmpeg も要らない形で確かめる


def test_the_centroid_lands_on_the_changed_block_in_screen_pixels(tool: ModuleType) -> None:
    """重心は縮めた絵の升目ではなく、画面の px で返す

    升目のまま返すと、予想の点（画面の px）と 4 倍ずれた所を比べ、どの枠も
    左上へ寄った読みになる
    """
    baseline = np.zeros((tool.COMPARE_HEIGHT, tool.COMPARE_WIDTH, 3), dtype=np.float32)
    picture = baseline.copy()
    # 縮めた絵の (100〜109, 40〜49) を変える 画面では x 400〜440、y 160〜200
    picture[40:50, 100:110] = 255.0
    found = tool.change_centroid(picture, baseline)
    assert found is not None
    x, y, count = found
    assert (x, y) == pytest.approx((420.0, 180.0))
    assert count == 100


def test_compression_noise_below_the_threshold_does_not_pull_the_centroid(
    tool: ModuleType,
) -> None:
    # 閾値より下の揺れまで数えると、動かしていない所まで重心に入り、真ん中へ寄る
    baseline = np.zeros((tool.COMPARE_HEIGHT, tool.COMPARE_WIDTH, 3), dtype=np.float32)
    picture = baseline + tool.MESH_THRESHOLD - 1.0
    assert tool.change_centroid(picture, baseline) is None
    picture[0:10, 0:10] = 255.0
    found = tool.change_centroid(picture, baseline)
    assert found is not None
    assert found[0] < 50.0 and found[1] < 50.0


def test_the_reading_tells_rows_from_columns(tool: ModuleType) -> None:
    # 3x3 の 1 番は、行ごとなら上の真ん中、列ごとなら左の真ん中
    by_row = tool.grid_position(1, 3, 3, by_row=True)
    by_column = tool.grid_position(1, 3, 3, by_row=False)
    assert by_row == (tool.WIDTH / 2, 0.0)
    assert by_column == (0.0, tool.HEIGHT / 2)
    assert tool.mesh_reading((980.0, 200.0), by_row, by_column) == "行ごと"
    assert tool.mesh_reading((300.0, 560.0), by_row, by_column) == "列ごと"
    # 何も変わらなければ、どちらとも言わない 言うと、書き出しの失敗が並びの答えに化ける
    assert tool.mesh_reading(None, by_row, by_column) == "変化なし"
    # 真ん中の点はどちらの並びでも同じ所なので、見分けたことにしない
    center = tool.grid_position(12, 5, 5, by_row=True)
    assert center == tool.grid_position(12, 5, 5, by_row=False)
    assert tool.mesh_reading((960.0, 540.0), center, center) == "見分けない"


def test_the_probe_moves_both_an_edge_row_point_and_an_edge_column_point(
    tool: ModuleType,
) -> None:
    """真ん中だけを動かすと、行ごとでも列ごとでも真ん中に出て見分けられない"""
    slots = tool.build_mesh_slots()
    moved = {(slot.columns, slot.rows, slot.moved) for slot in slots}
    assert {(3, 3, 4), (3, 3, 1), (3, 3, 3), (5, 5, 12), (3, 3, None)} <= moved
    starts = [slot.start for slot in slots]
    for before, after in itertools.pairwise(starts):
        assert after - before >= tool.MESH_SLOT + tool.GAP


def test_every_moved_slot_is_read_against_an_unmoved_slot_of_the_same_grid(
    tool: ModuleType,
) -> None:
    # 別の格子の基準と比べると、何も動かさなくても分け方の違いが差に出るかもしれない
    slots = tool.build_mesh_slots()
    manifest = tool.mesh_manifest(slots, Path("grid.png"))
    assert len(manifest["slots"]) == len(slots)
    for entry, slot in zip(manifest["slots"], slots, strict=True):
        base = slots[entry["baseline"]]
        assert base.moved is None
        assert (base.columns, base.rows) == (slot.columns, slot.rows)
        assert entry["item"]["Frame"] == slot.start
        assert ("by_row" in entry) == (slot.moved is not None)


def test_the_probe_effect_has_the_shape_of_the_real_one(tool: ModuleType) -> None:
    """``.work/probes/samples.json`` の実物と同じ項目 形を推測すると YMM4 が読み飛ばす"""
    slot = next(slot for slot in tool.build_mesh_slots() if slot.moved == 1)
    effect = tool.mesh_effect_entry(slot)
    assert set(effect) == {
        "$type",
        "HorizontalCount",
        "VerticalCount",
        "Points",
        "IsEnabled",
        "Remark",
    }
    assert effect["$type"] == (
        "YukkuriMovieMaker.Project.Effects.MeshDeformationEffect, YukkuriMovieMaker"
    )
    points = effect["Points"]
    assert len(points) == 9
    assert all(set(point) == {"X", "Y", "IsSelected"} for point in points)
    # 実物は最初の点だけが選ばれている
    assert [point["IsSelected"] for point in points] == [True] + [False] * 8
    moved = [index for index, point in enumerate(points) if point["X"]["Values"][0]["Value"]]
    assert moved == [1]


def test_the_probe_effect_is_read_by_the_mapper_it_is_meant_to_check(tool: ModuleType) -> None:
    """探りの JSON が今の写し方で読めないと、Sashimono の側が何も歪まず比べられない

    動かした点は、今の写し方（行ごと・Y を反す）どおり格子の 1 番へ入る
    """
    from sashimono.compat.aviutl.report import CompatibilityReport
    from sashimono.compat.ymm4.decorations import map_video_effects

    slot = next(slot for slot in tool.build_mesh_slots() if slot.moved == 1)
    report = CompatibilityReport()
    (mapped,) = map_video_effects([tool.mesh_effect_entry(slot)], report, length=30).effects
    assert not report.lines()
    grid = mapped.params["grid"]
    assert isinstance(grid, tuple)
    shift_x, shift_y = tool.MESH_SHIFT
    assert grid[2 + 2 : 2 + 4] == (shift_x, -shift_y)


def test_the_mesh_probe_project_has_the_bom_and_an_absolute_image_path(
    tool: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """BOM が無いと YMM4 は開けない 相対のパスだと下地が見つからず全部の枠が空になる

    空の書き出しを測ると「どの点を動かしても何も変わらない」と読めてしまう
    """
    monkeypatch.chdir(tmp_path)
    work = tmp_path / "work"
    (work / "images").mkdir(parents=True)
    (work / "mesh-probe.mp4").write_bytes(b"")
    (work / "mesh-report.json").write_text("{}", encoding="utf-8")
    (work / "images" / "mesh-01.png").write_bytes(b"")
    assert tool.command_mesh_build(SimpleNamespace(work=Path("work"))) == 0
    out = capsys.readouterr().out
    assert "mesh-probe.mp4" in out
    assert "前の探りの書き出し" in out
    raw = (work / "mesh-probe.ymmp").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    items = json.loads(raw.decode("utf-8-sig"))["Timelines"][0]["Items"]
    manifest = json.loads((work / "mesh-probe.json").read_text(encoding="utf-8"))
    assert len(items) == len(manifest["slots"])
    (image,) = {item["FilePath"] for item in items}
    assert Path(image).is_absolute()
    assert Path(image).is_file()
    # 前の測り結果と並べた絵は捨てる 新しい枠の一覧に対応しない
    assert not (work / "mesh-report.json").exists()
    assert not (work / "images" / "mesh-01.png").exists()


def _mesh_manifest_and_video(
    tool: ModuleType, work: Path, *, newer: int = 60, content: bytes = b""
) -> Path:
    """枠の一覧と、書き出し（既定は中身の無いもの）を置く 時刻の扱いは音の探りと同じ"""
    manifest = work / "mesh-probe.json"
    manifest.write_text(
        json.dumps(tool.mesh_manifest(tool.build_mesh_slots(), work / "grid.png")),
        encoding="utf-8",
    )
    return _export_after(manifest, work / "mesh-probe.mp4", newer=newer, content=content)


def test_measuring_the_mesh_before_the_export_explains_itself(
    tool: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # 書き出しは手作業 まだ無いときに例外で落ちると、手順の誤りか道具の故障か分からない
    arguments = SimpleNamespace(work=tmp_path)
    assert tool.command_mesh_measure(arguments) == 0
    assert "mesh-build" in capsys.readouterr().out
    (tmp_path / "mesh-probe.json").write_text(
        json.dumps(tool.mesh_manifest(tool.build_mesh_slots(), tmp_path / "grid.png")),
        encoding="utf-8",
    )
    assert tool.command_mesh_measure(arguments) == 0
    assert "まだ書き出されていません" in capsys.readouterr().out


@pytest.mark.parametrize("newer", [-60, 0])
def test_a_mesh_export_not_newer_than_the_probe_is_refused(
    tool: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str], newer: int
) -> None:
    """探りを作り直した後の古い書き出しを測ると、別の点を動かした絵の表が出る

    同じ時刻も断る 置き場によっては時刻が 2 秒刻みでしか残らない
    """
    _mesh_manifest_and_video(tool, tmp_path, newer=newer)
    (tmp_path / "mesh-report.json").write_text("{}", encoding="utf-8")
    assert tool.command_mesh_measure(SimpleNamespace(work=tmp_path)) == 0
    assert "作り直す前の書き出し" in capsys.readouterr().out
    assert not (tmp_path / "mesh-report.json").exists()


def test_a_mesh_export_without_pictures_explains_itself(
    tool: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 映像の道が無い書き出しで添字の例外に落ちると、書き出し直す案内が出ない
    _mesh_manifest_and_video(tool, tmp_path)
    monkeypatch.setattr(tool, "has_video_stream", lambda _video: False)
    assert tool.command_mesh_measure(SimpleNamespace(work=tmp_path)) == 0
    assert "映像の道がありません" in capsys.readouterr().out


#: 書き出し中や中断した後の mp4 に似せた、頭の読めないバイト列
#: ``moov`` が無いので ``av.open`` が ``InvalidDataError`` を投げる
UNFINISHED_EXPORT = b"\x00\x00\x00\x18ftypisom" + b"\xde\xad\xbe\xef" * 64


def test_an_unfinished_mesh_export_explains_itself_instead_of_a_traceback(
    tool: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """書き出しの途中で走らせると、時刻の検査は通り ``av.open`` が例外を投げる

    捕まえないと案内の無い traceback で終わり、書き出しを待てばよいのか
    道具が壊れたのか分からない
    """
    _mesh_manifest_and_video(tool, tmp_path, content=UNFINISHED_EXPORT)
    assert tool.command_mesh_measure(SimpleNamespace(work=tmp_path)) == 0
    assert "書き出しが終わっていないか壊れています" in capsys.readouterr().out
    assert not (tmp_path / "mesh-report.json").exists()


def test_a_mesh_export_that_breaks_partway_explains_itself(
    tool: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """頭は開けても途中で切れた書き出しは、フレームを読み進めた所で復号が失敗する

    開けるかどうかだけを守っても、そこで traceback になる
    """
    import av.error

    def broken(_video: Path) -> Iterator[tuple[int, Callable[[], np.ndarray]]]:
        yield 0, lambda: np.zeros((1080, 1920, 3), dtype=np.uint8)
        raise av.error.InvalidDataError(1094995529, "Invalid data found when processing input")

    _mesh_manifest_and_video(tool, tmp_path)
    monkeypatch.setattr(tool, "has_video_stream", lambda _video: True)
    monkeypatch.setattr(tool, "_ymm4_frames", broken)
    assert tool.command_mesh_measure(SimpleNamespace(work=tmp_path)) == 0
    assert "書き出しが終わっていないか壊れています" in capsys.readouterr().out


def test_an_unfinished_audio_export_explains_itself_instead_of_a_traceback(
    tool: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """音の探りも同じ作り 書き出しの途中の mp4 で ``decode_audio`` が例外を投げる"""
    _manifest_and_video(tool, tmp_path, content=UNFINISHED_EXPORT)
    (tmp_path / "audio-report.json").write_text("{}", encoding="utf-8")
    assert tool.command_audio_measure(SimpleNamespace(work=tmp_path)) == 0
    assert "書き出しが終わっていないか壊れています" in capsys.readouterr().out
    assert not (tmp_path / "audio-report.json").exists()


def test_ymm4_slots_come_back_in_manifest_order_though_read_in_time_order(
    tool: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """動画は頭から順にしか読めない 一覧が時刻順でなくても、返す絵は一覧の順

    時刻順のまま返すと、基準の絵と動かした絵を取り違え、差の重心を読み違える
    """

    def picture(value: int) -> Callable[[], np.ndarray]:
        return lambda: np.full((1080, 1920, 3), value, dtype=np.uint8)

    frames = [(index, picture(index)) for index in range(100)]
    monkeypatch.setattr(tool, "_ymm4_frames", lambda _video: iter(frames))
    entries = [{"start": 60, "length": 10}, {"start": 0, "length": 10}]
    first, second = tool._read_ymm4_slots(Path("unused.mp4"), entries)
    assert first is not None and second is not None
    assert float(first[0, 0, 0]) == 65.0
    assert float(second[0, 0, 0]) == 5.0


# 動画アイテムの再生速度が絵をどう進めるかの探り（video-rate-build / video-rate-measure）
# 素材の絵はその場で合成する ffmpeg も YMM4 も要らない


def _sources(count: int = 40) -> np.ndarray:
    """フレームごとに違う絵 隣どうしでも見分けがつく乱数の模様"""
    generator = np.random.default_rng(7)
    return generator.uniform(0.0, 255.0, size=(count, 9, 16, 3)).astype(np.float32)


def _played(sources: np.ndarray, rate: float, length: int) -> list[np.ndarray | None]:
    """``rate`` 倍で素材を進めた枠 素材を読み切った後は最後の絵で止め、圧縮の揺れを乗せる"""
    generator = np.random.default_rng(11)
    last = len(sources) - 1
    return [
        sources[min(int(elapsed * rate), last)] + generator.normal(0.0, 2.0, sources[0].shape)
        for elapsed in range(length)
    ]


@pytest.mark.parametrize("rate", [1.0, 0.5, 2.0])
def test_the_slope_reads_the_playback_rate(tool: ModuleType, rate: float) -> None:
    """経過フレームに対する素材のフレーム番号の傾きが、速さそのものになる

    読み切った後の止まった尾まで含めると、200% の傾きが 2 より小さく出て
    「YMM4 の絵は速さのとおりに進まない」と読み違える
    """
    sources = _sources()
    indices = [found for found, _ in tool.match_frames(_played(sources, rate, 60), sources)]
    slope = tool.rate_slope(indices, len(sources) - 1)
    assert slope == pytest.approx(rate, abs=0.02)


def test_a_stopped_picture_reads_as_stopped(tool: ModuleType) -> None:
    """再生速度 0 で同じ絵が続くと「止まる」 傾きを求められないと扱うと、
    止まったのか絵が出ていないのか見分けられない
    """
    sources = _sources()
    indices = [found for found, _ in tool.match_frames(_played(sources, 0.0, 60), sources)]
    assert set(indices) == {0}
    slope = tool.rate_slope(indices, len(sources) - 1)
    assert slope == 0.0
    assert tool.rate_reading(slope) == "止まる"


def test_a_picture_stopped_on_the_last_frame_reads_as_stopped(tool: ModuleType) -> None:
    """最後のフレームだけが並んだ枠も「止まる」

    最後のフレームで切ると点が残らず「絵が無い」と読み、一致数は揃っているのに
    表の中で食い違う（0 で素材の末尾から映す枠や、最後の絵を出し続ける枠）
    """
    sources = _sources()
    last = len(sources) - 1
    pictures: list[np.ndarray | None] = [sources[last].copy() for _ in range(30)]
    indices = [found for found, _ in tool.match_frames(pictures, sources)]
    assert set(indices) == {last}
    slope = tool.rate_slope(indices, last)
    assert slope == 0.0
    assert tool.rate_reading(slope) == "止まる"


def test_a_black_frame_is_not_taken_for_a_dark_source_frame(tool: ModuleType) -> None:
    """素材のどれにも似ていない絵（黒）は番号を持たない

    一番近い物をそのまま番号にすると、絵が出ていない枠が素材の暗いフレームとして
    数えられ、「止まる」と読み違える
    """
    sources = _sources()
    black = [np.zeros_like(sources[0]) for _ in range(30)]
    indices = [found for found, _ in tool.match_frames(black, sources)]
    assert indices == [None] * 30
    slope = tool.rate_slope(indices, len(sources) - 1)
    assert slope is None
    assert tool.rate_reading(slope) == "絵が無い"


def test_the_slope_ignores_frames_missing_from_the_export(tool: ModuleType) -> None:
    # 書き出しの頭が欠けても（動画に無い枠は None）、残りから同じ傾きを読む
    sources = _sources()
    pictures = _played(sources, 0.5, 60)
    pictures[:5] = [None] * 5
    indices = [found for found, _ in tool.match_frames(pictures, sources)]
    assert tool.rate_slope(indices, len(sources) - 1) == pytest.approx(0.5, abs=0.02)


def test_the_rate_row_reads_both_sides_the_same_way(tool: ModuleType) -> None:
    """YMM4 と Sashimono を同じ読み方で並べる 片方だけ読み方が違うと、差が作り物になる"""
    sources = _sources()
    entry = {"index": 0, "name": "PlaybackRate=50", "rate": 50.0}
    sides = [("ymm4", _played(sources, 0.5, 40)), ("sashimono", _played(sources, 1.0, 40))]
    row = tool.rate_row(entry, sides, sources, tool.RATE_MATCH_LIMIT)
    assert row["ymm4"]["reading"] == "0.50 倍"
    assert row["sashimono"]["reading"] == "1.00 倍"
    assert row["ymm4"]["first"] == 0
    assert row["ymm4"]["matched"] == 40


def test_the_rate_slots_never_overlap_and_outlast_the_source(tool: ModuleType) -> None:
    """枠は重ならず、素材より長い 短いと 50% で素材を読み進める途中で枠が終わり、
    読み切った後の絵（止まるのか消えるのか）も見えない
    """
    slots = tool.build_rate_slots()
    assert [slot.rate for slot in slots[:4]] == [100.0, 50.0, 200.0, 0.0]
    for before, after in itertools.pairwise(slots):
        assert after.start >= before.start + tool.RATE_SLOT + tool.RATE_GAP
    assert tool.RATE_SLOT > tool.RATE_SOURCE_SECONDS * tool.FPS


def test_the_rate_item_writes_both_rate_fields_like_the_newer_ymm4(tool: ModuleType) -> None:
    """実物の新しい版の動画アイテムは ``PlaybackRate`` と ``PlaybackRate2`` が同じ値

    片方だけ変えると、YMM4 がどちらを読んだのか書き出しから読めない
    """
    slot = tool.build_rate_slots()[1]
    item = tool.rate_video_item(slot, Path("C:/work/source.mp4"))
    assert item["$type"].startswith("YukkuriMovieMaker.Project.Items.VideoItem")
    assert item["PlaybackRate"] == slot.rate
    assert item["PlaybackRate2"]["Values"] == [{"Value": slot.rate}]
    assert item["PlaybackRateAudioProcessingMode"] == "Resampling"
    assert item["Length"] == tool.RATE_SLOT


def _no_ffmpeg(tool: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tool.shutil, "which", lambda _name: None)


def test_the_rate_build_without_ffmpeg_explains_itself(
    tool: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ffmpeg が無い機械で例外で落ちると、道具が壊れたのか環境なのか分からない"""
    _no_ffmpeg(tool, monkeypatch)
    (tmp_path / "video-rate-report.json").write_text("{}", encoding="utf-8")
    assert tool.command_video_rate_build(SimpleNamespace(work=tmp_path)) == 0
    assert "ffmpeg" in capsys.readouterr().out
    assert not (tmp_path / "video-rate-probe.ymmp").exists()
    # 作れなかった道でも前の表を残さない 残ると、作り直した探りの結果として開けてしまう
    assert not (tmp_path / "video-rate-report.json").exists()


def test_the_rate_build_without_libx264_says_so(
    tool: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # libx264 の無い ffmpeg がある 失敗の理由を言わないと、ffmpeg を入れ直しても直らない
    monkeypatch.setattr(tool.shutil, "which", lambda _name: "ffmpeg")
    failed = SimpleNamespace(returncode=1, stderr=b"Unknown encoder 'libx264'\n")
    monkeypatch.setattr(tool.subprocess, "run", lambda *_a, **_k: failed)
    assert "libx264" in tool.make_rate_source(tmp_path / "source.mp4")


def test_the_rate_probe_writes_an_absolute_path_for_the_source(
    tool: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """相対のまま書くと YMM4 が素材を見つけられず、全部の枠が黒になる

    黒を測ると「どの速さでも絵が出ない」と読めてしまう 前の書き出しが残っていれば言う
    """
    monkeypatch.setattr(tool, "make_rate_source", lambda target: (target.write_bytes(b""), "")[1])
    monkeypatch.chdir(tmp_path)
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / "video-rate-probe.mp4").write_bytes(b"")
    assert tool.command_video_rate_build(SimpleNamespace(work=Path("work"))) == 0
    assert "前の探りの書き出し" in capsys.readouterr().out
    raw = (tmp_path / "work" / "video-rate-probe.ymmp").read_bytes().decode("utf-8-sig")
    paths = [item["FilePath"] for item in json.loads(raw)["Timelines"][0]["Items"]]
    assert len(paths) == len(tool.build_rate_slots())
    assert all(Path(path).is_absolute() for path in paths)


def _rate_manifest_and_video(
    tool: ModuleType, work: Path, *, newer: int = 60, content: bytes = b""
) -> Path:
    """枠の一覧と、素材と書き出しを置く 素材は ``media`` に作る 時刻の扱いは音の探りと同じ"""
    manifest = work / "video-rate-probe.json"
    manifest.write_text(
        json.dumps(tool.rate_manifest(tool.build_rate_slots(), work / "video-rate-source.mp4")),
        encoding="utf-8",
    )
    return _export_after(manifest, work / "video-rate-probe.mp4", newer=newer, content=content)


def test_measuring_the_rate_before_building_explains_itself(
    tool: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    arguments = SimpleNamespace(work=tmp_path, skip_sashimono=True)
    assert tool.command_video_rate_measure(arguments) == 0
    assert "video-rate-build" in capsys.readouterr().out


@pytest.mark.parametrize("newer", [-60, 0])
def test_a_rate_export_not_newer_than_the_probe_is_refused(
    tool: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str], newer: int
) -> None:
    """作り直した探りで古い書き出しを測ると、別の速さを測った表が出る

    同じ時刻も断る 置き場によっては時刻が 2 秒刻みでしか残らない
    """
    _rate_manifest_and_video(tool, tmp_path, newer=newer)
    arguments = SimpleNamespace(work=tmp_path, skip_sashimono=True)
    assert tool.command_video_rate_measure(arguments) == 0
    assert "作り直す前の書き出し" in capsys.readouterr().out


def test_a_broken_rate_export_explains_itself_and_drops_the_old_report(
    tool: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """書き出しの途中や壊れた mp4 で traceback が出ると、測り方の案内が出ない

    前の表も残さない 残ると、新しい結果として開けてしまう
    """
    (tmp_path / "video-rate-source.mp4").write_bytes(b"")
    monkeypatch.setattr(tool, "_read_source", lambda _media: _sources())
    _rate_manifest_and_video(tool, tmp_path, content=b"not a movie")
    (tmp_path / "video-rate-report.json").write_text("{}", encoding="utf-8")
    arguments = SimpleNamespace(work=tmp_path, skip_sashimono=True)
    assert tool.command_video_rate_measure(arguments) == 0
    assert "書き出しが終わっていないか壊れています" in capsys.readouterr().out
    assert not (tmp_path / "video-rate-report.json").exists()


def test_a_broken_rate_source_explains_itself(
    tool: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # 素材が壊れていると全部の枠が「絵が無い」になり、YMM4 が絵を出さないと読み違える
    (tmp_path / "video-rate-source.mp4").write_bytes(b"not a movie")
    _rate_manifest_and_video(tool, tmp_path)
    arguments = SimpleNamespace(work=tmp_path, skip_sashimono=True)
    assert tool.command_video_rate_measure(arguments) == 0
    assert "video-rate-build を走らせ直して" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("command", "maker", "failed", "stem", "report"),
    [
        ("command_audio_build", "make_tone", False, "audio-probe", "audio-report.json"),
        (
            "command_video_rate_build",
            "make_rate_source",
            "ffmpeg が無い",
            "video-rate-probe",
            "video-rate-report.json",
        ),
    ],
)
def test_a_failed_rebuild_drops_the_old_probe_so_it_is_not_measured_as_new(
    tool: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    maker: str,
    failed: object,
    stem: str,
    report: str,
) -> None:
    """作り直しに失敗したら、前の一覧とプロジェクトも捨てる

    残すと、前に成功していた作業フォルダでは measure が古い一覧と古い書き出しの組を
    今回の物として測る 書き出しは本人が作った物なので残す
    """
    for name in (f"{stem}.json", f"{stem}.ymmp", f"{stem}.mp4", report):
        (tmp_path / name).write_text("{}", encoding="utf-8")
    monkeypatch.setattr(tool, maker, lambda _target: failed)
    assert getattr(tool, command)(SimpleNamespace(work=tmp_path)) == 0
    assert not (tmp_path / f"{stem}.json").exists()
    assert not (tmp_path / f"{stem}.ymmp").exists()
    assert not (tmp_path / report).exists()
    assert (tmp_path / f"{stem}.mp4").exists()


def _refuse_reading(*_args: object) -> None:
    raise AssertionError("フレームレートの違う書き出しを読み進めた")


def test_a_mesh_export_at_another_frame_rate_is_refused(
    tool: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一覧の枠の番号は 30fps で数えている 60fps の書き出しにそのまま当てると、
    枠の真ん中のつもりで前の方の絵を切り出し、別の点を動かした絵を測る
    """
    _mesh_manifest_and_video(tool, tmp_path)
    monkeypatch.setattr(tool, "has_video_stream", lambda _video: True)
    monkeypatch.setattr(tool, "export_fps", lambda _video: 60.0)
    monkeypatch.setattr(tool, "_read_ymm4_slots", _refuse_reading)
    assert tool.command_mesh_measure(SimpleNamespace(work=tmp_path)) == 0
    assert "一覧は 30fps 書き出しは 60fps" in capsys.readouterr().out
    assert not (tmp_path / "mesh-report.json").exists()


def test_a_rate_export_at_another_frame_rate_is_refused(
    tool: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 60fps の書き出しを 30fps の番号で数えると、等倍が 0.5 倍と出る
    (tmp_path / "video-rate-source.mp4").write_bytes(b"")
    monkeypatch.setattr(tool, "_read_source", lambda _media: _sources())
    _rate_manifest_and_video(tool, tmp_path)
    monkeypatch.setattr(tool, "has_video_stream", lambda _video: True)
    monkeypatch.setattr(tool, "export_fps", lambda _video: 60.0)
    monkeypatch.setattr(tool, "_read_rate_slots", _refuse_reading)
    arguments = SimpleNamespace(work=tmp_path, skip_sashimono=True)
    assert tool.command_video_rate_measure(arguments) == 0
    assert "一覧は 30fps 書き出しは 60fps" in capsys.readouterr().out
    assert not (tmp_path / "video-rate-report.json").exists()


# PlaybackRate と PlaybackRate2 を食い違わせた枠（Issue #117）


def test_the_mismatched_rate_slots_follow_the_old_ones_so_old_results_still_line_up(
    tool: ModuleType,
) -> None:
    """食い違わせた枠を間に挟むと、前の測り結果と枠の番号と位置がずれて比べられない"""
    slots = tool.build_rate_slots()
    old = slots[: len(tool.RATE_CONDITIONS)]
    step = tool.RATE_SLOT + tool.RATE_GAP
    assert [(slot.name, slot.rate, slot.start) for slot in old] == [
        (f"PlaybackRate={rate:g}", rate, index * step)
        for index, rate in enumerate(tool.RATE_CONDITIONS)
    ]
    # 前からある枠は、前と同じく両方に同じ速さを書く
    for slot in old:
        item = tool.rate_video_item(slot, Path("C:/work/source.mp4"))
        assert item["PlaybackRate2"] == {
            "Values": [{"Value": slot.rate}],
            "Span": 0.0,
            "AnimationType": "なし",
        }
    added = slots[len(tool.RATE_CONDITIONS) :]
    assert [(slot.name, slot.rate, slot.second) for slot in added] == [
        ("PlaybackRate=100 PlaybackRate2=50", 100.0, (50.0,)),
        ("PlaybackRate=50 PlaybackRate2=100", 50.0, (100.0,)),
        ("PlaybackRate=100 PlaybackRate2=50→200", 100.0, (50.0, 200.0)),
    ]
    for before, after in itertools.pairwise(slots):
        assert after.start >= before.start + step


def test_the_mismatched_rate_items_carry_both_values_into_the_project(
    tool: ModuleType, tmp_path: Path
) -> None:
    # 一覧だけ食い違っていて .ymmp が同じ値だと、YMM4 はどちらも同じに読み、何も分からない
    slots = tool.build_rate_slots()
    manifest = tool.rate_manifest(slots, tmp_path / "source.mp4")
    target = tmp_path / "video-rate-probe.ymmp"
    tool.write_document([entry["item"] for entry in manifest["slots"]], 2000, target)
    items = json.loads(target.read_bytes().decode("utf-8-sig"))["Timelines"][0]["Items"]
    by_name = {item["Remark"]: item for item in items}
    first = by_name["PlaybackRate=100 PlaybackRate2=50"]
    assert first["PlaybackRate"] == 100.0
    assert first["PlaybackRate2"]["Values"] == [{"Value": 50.0}]
    second = by_name["PlaybackRate=50 PlaybackRate2=100"]
    assert second["PlaybackRate"] == 50.0
    assert second["PlaybackRate2"]["Values"] == [{"Value": 100.0}]
    entry = manifest["slots"][-1]
    assert entry["rate"] == 100.0
    assert entry["rate2"] == [50.0, 200.0]


def test_the_moving_rate2_is_written_like_the_real_moving_values(tool: ModuleType) -> None:
    """動く値は実物（この機械のプロジェクトの ``Zoom``）と同じ形で書く

    ``KeyFrames`` を空にして ``Values`` を 2 つ ``直線移動`` で持つ 推測の形で書くと
    YMM4 が開けないか、止まった値として読んで何も測れない こちらの読み込みも
    同じ形を頭から終わりへ動く値と読むので、YMM4 と同じ意味で書けている
    """
    from sashimono.compat.ymm4.values import animated

    slot = tool.build_rate_slots()[-1]
    item = tool.rate_video_item(slot, Path("C:/work/source.mp4"))
    moving = item["PlaybackRate2"]
    assert moving["Values"] == [{"Value": 50.0}, {"Value": 200.0}]
    assert moving["AnimationType"] == "直線移動"
    assert moving["Span"] == 0.0
    assert moving["Bezier"]["Points"][1]["Point"] == {"X": 1.0, "Y": 1.0}
    assert item["KeyFrames"] == {"Frames": [], "Count": 0}
    assert item["PlaybackRate"] == 100.0
    read = animated(moving, 100.0, length=item["Length"], keyframes=item["KeyFrames"])
    assert [(point.frame, point.value) for point in read.keyframes] == [
        (0, 50.0),
        (tool.RATE_SLOT, 200.0),
    ]


def _accumulated(values: tuple[float, float], length: int, last: int) -> list[int | None]:
    """フレームごとに速さを積み上げて素材を進めた番号 読み切ったら最後で止める"""
    first, final = values
    position = 0.0
    indices: list[int | None] = []
    for frame in range(length):
        indices.append(min(int(position), last))
        position += (first + (final - first) * frame / length) / 100.0
    return indices


def test_window_slopes_show_the_speed_changing_midway(tool: ModuleType) -> None:
    """全体の傾き 1 つでは、途中で速さが変わったのか一定なのか見分けられない

    50→200 と動いた速さも、全体の傾きでは 1 倍前後の一定の速さと同じに見える
    """
    length, last = tool.RATE_SLOT, 400
    indices = _accumulated((50.0, 200.0), length, last)
    windows = tool.window_slopes(indices, last)
    assert len(windows) == length // tool.RATE_WINDOW
    assert all(value is not None for value in windows)
    assert windows == sorted(windows)
    expected = tool.expected_windows([50.0, 200.0], length)
    assert windows == pytest.approx(expected, abs=0.03)
    entry = {"length": length, "rate": 100.0, "rate2": [50.0, 200.0]}
    nearer, gaps = tool.nearer_rate(windows, tool.rate_expectations(entry))
    assert nearer == "PlaybackRate2"
    assert gaps["PlaybackRate2"] < gaps["PlaybackRate"]
    steady = tool.window_slopes(_accumulated((100.0, 100.0), length, last), last)
    assert tool.nearer_rate(steady, tool.rate_expectations(entry))[0] == "PlaybackRate"


def test_a_window_after_the_source_runs_out_is_not_read_as_stopped(tool: ModuleType) -> None:
    """読み切った後の区切りを 0 と読むと、PlaybackRate2 が途中で 0 へ落ちたように見える"""
    last = 60
    indices = _accumulated((200.0, 200.0), 90, last)
    windows = tool.window_slopes(indices, last)
    assert windows[0] == pytest.approx(2.0, abs=0.05)
    assert windows[-1] is None


def test_the_nearer_value_is_read_for_a_still_mismatch(tool: ModuleType) -> None:
    entry = {"length": 60, "rate": 100.0, "rate2": [50.0]}
    expectations = tool.rate_expectations(entry)
    assert tool.nearer_rate([0.5, 0.49], expectations)[0] == "PlaybackRate2"
    assert tool.nearer_rate([1.0, 1.01], expectations)[0] == "PlaybackRate"
    # どちらとも違う（止まった）ときに近い方を名指すと、効いた値を取り違える
    assert tool.nearer_rate([0.0, 0.0], expectations)[0] == "どちらとも合わない"
    assert tool.nearer_rate([None, None], expectations)[0] == "測れない"


def test_an_old_slot_says_it_cannot_tell_the_two_apart(tool: ModuleType) -> None:
    """同じ値を書いた枠で近い方を名指すと、測っていない読み方を測ったように見せる

    前の版の一覧（``rate2`` が無い）も同じ値を書いていた枠として読む
    """
    entry = {"length": 60, "rate": 50.0}
    nearer, _ = tool.nearer_rate([0.5, 0.5], tool.rate_expectations(entry))
    assert nearer == "見分けられない（同じ値）"


def test_the_rate_row_and_table_show_both_guesses_for_a_mismatch(
    tool: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    sources = _sources(200)
    entry = {
        "index": 6,
        "name": "PlaybackRate=100 PlaybackRate2=50→200",
        "rate": 100.0,
        "rate2": [50.0, 200.0],
        "length": 120,
    }
    last = len(sources) - 1
    played: list[np.ndarray | None] = [
        None if index is None else sources[index]
        for index in _accumulated((50.0, 200.0), 120, last)
    ]
    sides = [("ymm4", played), ("sashimono", _played(sources, 1.0, 120))]
    row = tool.rate_row(entry, sides, sources, tool.RATE_MATCH_LIMIT)
    assert row["ymm4"]["nearer"] == "PlaybackRate2"
    assert row["sashimono"]["nearer"] == "PlaybackRate"
    # 素材との突き合わせの差は前と同じ名前のまま残る
    assert len(row["ymm4"]["distances"]) == 120
    tool._print_rate_rows([row])
    out = capsys.readouterr().out
    assert "PlaybackRate2 予想" in out
    assert "→ PlaybackRate2" in out


# 新しい版の音声アイテム（Issue #117）

#: この機械のプロジェクトにあった新しい版の AudioItem 42 個の項目の並び
NEWER_AUDIO_KEYS = [
    "$type",
    "IsWaveformEnabled",
    "FilePath",
    "AudioTrackIndex",
    "Volume",
    "Pan",
    "PlaybackRate2",
    "PlaybackRateAudioProcessingMode",
    "ContentOffset",
    "FadeIn",
    "FadeOut",
    "IsLooped",
    "EchoIsEnabled",
    "EchoInterval",
    "EchoAttenuation",
    "AudioEffects",
    "Group",
    "Frame",
    "Layer",
    "KeyFrames",
    "Length",
    "PlaybackRate",
    "Remark",
    "IsLocked",
    "IsHidden",
]


def test_the_newer_audio_slots_follow_the_old_ones_in_the_old_form(tool: ModuleType) -> None:
    """前からある枠の形や位置を変えると、前の測り結果と比べられない"""
    slots = tool.build_audio_slots()
    old = [slot for slot in slots if slot.playback_rate2 is None]
    assert slots[: len(old)] == old
    for slot in old:
        item = tool.audio_item(slot, Path("tone.wav"))
        assert "PlaybackRate2" not in item
        assert "PlaybackRateAudioProcessingMode" not in item
    added = slots[len(old) :]
    assert [(s.kind, s.playback_rate, s.playback_rate2, s.mode) for s in added] == [
        ("rate2", 100.0, 50.0, "Resampling"),
        ("rate2", 50.0, 100.0, "Resampling"),
        ("mode", 50.0, 50.0, "Sola"),
        ("mode", 200.0, 200.0, "Sola"),
    ]
    for before, after in itertools.pairwise(slots):
        assert after.start - before.start >= tool.AUDIO_SLOT + tool.AUDIO_GAP


def test_the_newer_audio_items_are_written_like_the_real_newer_ones(
    tool: ModuleType, tmp_path: Path
) -> None:
    # 形を推測すると YMM4 がプロジェクトを開けない 実物の新しい版の並びに合わせる
    every = tool.build_audio_slots()
    slots = [slot for slot in every if slot.playback_rate2 is not None]
    target = tmp_path / "audio-probe.ymmp"
    items = [tool.audio_item(slot, tmp_path / "tone.wav") for slot in slots]
    tool.write_document(items, 100, target)
    written = json.loads(target.read_bytes().decode("utf-8-sig"))["Timelines"][0]["Items"]
    for slot, item in zip(slots, written, strict=True):
        assert list(item) == NEWER_AUDIO_KEYS
        assert item["PlaybackRate"] == slot.playback_rate
        assert item["PlaybackRate2"]["Values"] == [{"Value": slot.playback_rate2}]
        assert item["PlaybackRateAudioProcessingMode"] == slot.mode
    manifest = tool.audio_manifest(every, tmp_path / "tone.wav")
    entries = [entry for entry in manifest["slots"] if entry["playback_rate2"] is not None]
    assert [(e["playback_rate"], e["playback_rate2"], e["mode"]) for e in entries] == [
        (100.0, 50.0, "Resampling"),
        (50.0, 100.0, "Resampling"),
        (50.0, 50.0, "Sola"),
        (200.0, 200.0, "Sola"),
    ]


def _sound_entry(tool: ModuleType, name: str) -> dict[str, object]:
    manifest = tool.audio_manifest(tool.build_audio_slots(), Path("tone.wav"))
    found: dict[str, object] = next(entry for entry in manifest["slots"] if entry["name"] == name)
    return found


def test_the_sound_says_which_rate_it_followed(tool: ModuleType) -> None:
    """長さと高さの両方で読む 長さだけだと、枠で頭打ちになった 4 秒を取り違える"""
    rate = 48000
    entry = _sound_entry(tool, "PlaybackRate=100 PlaybackRate2=50")
    base = tool.measure_block(tone(rate, 4.0), rate)
    # PlaybackRate2 の 50 が効いたなら 4 秒・220Hz
    slow = tool.measure_block(tone(rate, 4.0, hz=220.0), rate)
    assert tool._audio_row(entry, slow, base)["nearer"] == "PlaybackRate2"
    # PlaybackRate の 100 が効いたなら 2 秒・440Hz
    block = np.zeros((2, rate * 4), dtype=np.float32)
    block[:, : rate * 2] = tone(rate, 2.0)
    row = tool._audio_row(entry, tool.measure_block(block, rate), base)
    assert row["nearer"] == "PlaybackRate"
    assert set(row["expected"]) == {"PlaybackRate", "PlaybackRate2"}


def test_sola_is_told_apart_by_the_pitch_it_keeps(tool: ModuleType) -> None:
    """Sola と Resampling は長さが同じ 高さを見ないと見分けられない"""
    rate = 48000
    entry = _sound_entry(tool, "PlaybackRate=50 PlaybackRate2=50 Sola")
    base = tool.measure_block(tone(rate, 4.0), rate)
    kept = tool._audio_row(entry, tool.measure_block(tone(rate, 4.0), rate), base)
    assert kept["nearer"] == "高さを保つ"
    lowered = tool._audio_row(entry, tool.measure_block(tone(rate, 4.0, hz=220.0), rate), base)
    assert lowered["nearer"] == "高さも変わる"
    silent = tool.measure_block(np.zeros((2, rate * 4), dtype=np.float32), rate)
    assert tool._audio_row(entry, silent, base)["nearer"] == "鳴らない"


def test_the_old_audio_rows_carry_no_guess(
    tool: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    # 前からある枠に予想を付けると、前の表と列が変わって比べにくい
    rate = 48000
    base = tool.measure_block(tone(rate, 4.0), rate)
    manifest = tool.audio_manifest(tool.build_audio_slots(), Path("tone.wav"))
    rows = [tool._audio_row(entry, base, base) for entry in manifest["slots"]]
    assert ["expected" in row for row in rows] == [
        entry["playback_rate2"] is not None for entry in manifest["slots"]
    ]
    tool._print_audio_rows(rows)
    assert "新しい版の形" in capsys.readouterr().out
