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
    video = work / "audio-probe.mp4"
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
    video = work / "mesh-probe.mp4"
    video.write_bytes(content)
    stamp = manifest.stat()
    os.utime(video, (stamp.st_atime + newer, stamp.st_mtime + newer))
    return video


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
