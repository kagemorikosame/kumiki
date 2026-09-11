"""キャッシュの往復と、非同期解析。"""

from __future__ import annotations

import threading
import time
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest

from kumiki.engine.audio import analyze_waveform
from kumiki.engine.cache import (
    CacheStore,
    MediaAnalyzer,
    build_filmstrip,
    filmstrip_key,
    load_filmstrip,
    load_waveform,
    media_key,
    save_filmstrip,
    save_waveform,
    waveform_key,
)
from kumiki.engine.cache.store import load_arrays, save_arrays
from kumiki.engine.decode import probe_media
from tests.media_fixtures import SampleMedia


@pytest.fixture
def store(tmp_path: Path) -> CacheStore:
    return CacheStore(tmp_path / "cache")


class TestMediaKey:
    def test_same_file_same_key(self, sample_av: SampleMedia) -> None:
        assert media_key(sample_av.path) == media_key(sample_av.path)

    def test_conditions_change_the_key(self, sample_av: SampleMedia) -> None:
        # 解析条件が違えば結果も違う。同じ鍵にすると古い設定の結果を掴む。
        assert media_key(sample_av.path, extra="48000") != media_key(sample_av.path, extra="44100")

    def test_modified_file_changes_the_key(self, tmp_path: Path) -> None:
        path = tmp_path / "a.bin"
        path.write_bytes(b"x" * 100)
        before = media_key(path)
        path.write_bytes(b"x" * 200)
        assert media_key(path) != before

    def test_missing_file_still_produces_a_key(self, tmp_path: Path) -> None:
        assert media_key(tmp_path / "無い.mp4")


class TestStore:
    def test_shards_by_key_prefix(self, store: CacheStore) -> None:
        # 1 つのフォルダにファイルが数万個並ぶと走査が目に見えて遅くなる。
        path = store.path_for("things", "abcdef123456", ".dat")
        assert path.parent.name == "ab"
        assert path.name == "abcdef123456.dat"

    def test_array_round_trip(self, store: CacheStore) -> None:
        target = store.prepare("things", "key", ".npz")
        original: dict[str, np.ndarray] = {
            "a": np.arange(10),
            "b": np.ones((2, 3), dtype=np.float32),
        }
        save_arrays(target, original)

        loaded = load_arrays(target)
        assert loaded is not None
        assert np.array_equal(loaded["a"], original["a"])
        assert np.array_equal(loaded["b"], original["b"])

    def test_no_temporary_file_is_left(self, store: CacheStore) -> None:
        target = store.prepare("things", "key", ".npz")
        save_arrays(target, {"a": np.arange(3)})
        assert [p.name for p in target.parent.iterdir()] == ["key.npz"]

    def test_corrupt_file_is_discarded(self, store: CacheStore) -> None:
        # 壊れたキャッシュは黙って捨てる。作り直せるものなので、
        # ここでユーザーに何かを伝える意味は無い。
        target = store.prepare("things", "key", ".npz")
        target.write_bytes(b"broken")
        assert load_arrays(target) is None
        assert not target.exists()

    def test_missing_file(self, store: CacheStore) -> None:
        assert load_arrays(store.path_for("things", "nope", ".npz")) is None

    def test_clear(self, store: CacheStore) -> None:
        save_arrays(store.prepare("things", "key", ".npz"), {"a": np.arange(3)})
        assert store.size_bytes() > 0
        store.clear()
        assert store.size_bytes() == 0


class TestWaveformCache:
    def test_round_trip(self, store: CacheStore, sample_av: SampleMedia) -> None:
        waveform = analyze_waveform(sample_av.path)
        assert waveform is not None
        key = waveform_key(sample_av.path, 48000, 2)
        save_waveform(store, key, waveform)

        loaded = load_waveform(store, key)
        assert loaded is not None
        assert loaded.sample_rate == waveform.sample_rate
        assert loaded.channels == waveform.channels
        assert loaded.total_samples == waveform.total_samples
        assert len(loaded.levels) == len(waveform.levels)
        for restored, original in zip(loaded.levels, waveform.levels, strict=True):
            assert restored.samples_per_peak == original.samples_per_peak
            assert np.array_equal(restored.peaks, original.peaks)

    def test_missing_returns_none(self, store: CacheStore) -> None:
        assert load_waveform(store, "存在しない鍵") is None

    def test_version_mismatch_is_ignored(self, store: CacheStore, sample_av: SampleMedia) -> None:
        # 段階の作り方を変えたら、古いキャッシュは読まずに作り直す。
        waveform = analyze_waveform(sample_av.path)
        assert waveform is not None
        key = waveform_key(sample_av.path, 48000, 2)
        path = save_waveform(store, key, waveform)

        data = load_arrays(path)
        assert data is not None
        data["version"] = np.array([999])
        save_arrays(path, data)

        assert load_waveform(store, key) is None


class TestFilmstrip:
    def test_builds_tiles(self, sample_av: SampleMedia) -> None:
        filmstrip = build_filmstrip(sample_av.path, interval=Fraction(1, 2), height=24)
        assert filmstrip is not None
        # 2 秒を 0.5 秒間隔なので 4〜5 枚。
        assert 4 <= filmstrip.count <= 5
        assert filmstrip.height == 24
        # 320x240 を高さ 24 にすると幅は 32。
        assert filmstrip.tile_width == 32

    def test_tiles_differ_over_time(self, sample_av: SampleMedia) -> None:
        filmstrip = build_filmstrip(sample_av.path, interval=Fraction(1, 2), height=24)
        assert filmstrip is not None
        first, last = filmstrip.tile(0), filmstrip.tile(filmstrip.count - 1)
        assert first is not None
        assert last is not None
        assert not np.array_equal(first, last), "全部同じ絵になっている"

    def test_lookup_by_time(self, sample_av: SampleMedia) -> None:
        filmstrip = build_filmstrip(sample_av.path, interval=Fraction(1, 2), height=24)
        assert filmstrip is not None

        def same(seconds: int, index: int) -> bool:
            found, expected = filmstrip.at(Fraction(seconds)), filmstrip.tile(index)
            assert found is not None
            assert expected is not None
            return bool(np.array_equal(found, expected))

        assert same(0, 0)
        assert same(1, 2)
        # 範囲を越えたら最後の 1 枚で頭打ち。
        assert same(100, filmstrip.count - 1)

    def test_out_of_range_tile(self, sample_av: SampleMedia) -> None:
        filmstrip = build_filmstrip(sample_av.path, interval=Fraction(1, 2), height=24)
        assert filmstrip is not None
        assert filmstrip.tile(-1) is None
        assert filmstrip.tile(9999) is None

    def test_can_be_cancelled(self, sample_av: SampleMedia) -> None:
        assert build_filmstrip(sample_av.path, should_cancel=lambda: True) is None

    def test_broken_media_returns_none(self, tmp_path: Path) -> None:
        assert build_filmstrip(tmp_path / "無い.mp4") is None

    def test_round_trip(self, store: CacheStore, sample_av: SampleMedia) -> None:
        filmstrip = build_filmstrip(sample_av.path, interval=Fraction(1, 2), height=24)
        assert filmstrip is not None
        key = filmstrip_key(sample_av.path, Fraction(1, 2), 24)
        save_filmstrip(store, key, filmstrip)

        loaded = load_filmstrip(store, key)
        assert loaded is not None
        assert loaded.tile_width == filmstrip.tile_width
        assert loaded.interval == filmstrip.interval
        assert np.array_equal(loaded.sheet, filmstrip.sheet)


class TestMediaAnalyzer:
    def _wait_for(self, predicate: object, timeout: float = 60.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():  # type: ignore[operator]
                return True
            time.sleep(0.02)
        return False

    def test_produces_both_results(self, store: CacheStore, sample_av: SampleMedia) -> None:
        media = probe_media(sample_av.path)
        analyzer = MediaAnalyzer(store)
        try:
            ready = threading.Event()
            analyzer.request(media, on_ready=lambda _: ready.set())
            assert self._wait_for(
                lambda: (
                    analyzer.waveform(media) is not None and analyzer.filmstrip(media) is not None
                )
            ), "解析が終わらない"
        finally:
            analyzer.close()

    def test_lookup_never_blocks_before_ready(
        self, store: CacheStore, sample_av: SampleMedia
    ) -> None:
        # 描画のたびに呼ばれるので、未完了でも即座に返ること。
        media = probe_media(sample_av.path)
        analyzer = MediaAnalyzer(store)
        try:
            started = time.monotonic()
            assert analyzer.waveform(media) is None
            assert time.monotonic() - started < 0.05
        finally:
            analyzer.close()

    def test_second_run_uses_the_disk_cache(
        self, store: CacheStore, sample_av: SampleMedia
    ) -> None:
        media = probe_media(sample_av.path)
        analyzer = MediaAnalyzer(store)
        try:
            analyzer.request(media)
            assert self._wait_for(lambda: analyzer.waveform(media) is not None)
        finally:
            analyzer.close()

        # 別のインスタンスでも、同じ store を指していれば解析し直さない。
        assert load_waveform(store, waveform_key(sample_av.path, 48000, 2)) is not None

        analyzer = MediaAnalyzer(store)
        try:
            analyzer.request(media)
            assert self._wait_for(lambda: analyzer.waveform(media) is not None, timeout=10.0)
        finally:
            analyzer.close()

    def test_forget_drops_the_result(self, store: CacheStore, sample_av: SampleMedia) -> None:
        media = probe_media(sample_av.path)
        analyzer = MediaAnalyzer(store)
        try:
            analyzer.request(media)
            assert self._wait_for(lambda: analyzer.waveform(media) is not None)
            analyzer.forget(media.id)
            assert analyzer.waveform(media) is None
        finally:
            analyzer.close()

    def test_video_only_media_gets_no_waveform(
        self, store: CacheStore, sample_long: SampleMedia
    ) -> None:
        media = probe_media(sample_long.path)
        analyzer = MediaAnalyzer(store)
        try:
            analyzer.request(media)
            assert self._wait_for(lambda: analyzer.filmstrip(media) is not None)
            assert analyzer.waveform(media) is None
        finally:
            analyzer.close()

    def test_close_is_idempotent(self, store: CacheStore) -> None:
        analyzer = MediaAnalyzer(store)
        analyzer.close()
        analyzer.close()
