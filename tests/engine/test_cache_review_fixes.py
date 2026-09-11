"""CodeRabbit の初回レビュー（PR #8）で見つかった穴

`engine/cache/` は `.gitignore` に巻き込まれて一度も git に入っておらず、
レビューを受けたのは今回が初めてだった どれも手元では起きにくく、
起きたときに原因が見えにくい種類のもの
"""

from __future__ import annotations

import threading
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtGui import QSurfaceFormat

from kumiki.engine.audio import analyze_waveform
from kumiki.engine.cache import CacheStore, MediaAnalyzer, load_waveform, save_waveform
from kumiki.engine.cache.store import load_arrays, save_arrays
from kumiki.engine.decode import probe_media
from kumiki.engine.gpu.context import GLContextError, OffscreenGLContext
from tests.media_fixtures import SampleMedia


@pytest.fixture
def store(tmp_path: Path) -> CacheStore:
    return CacheStore(tmp_path / "cache")


class TestAnalyzerAfterClose:
    def test_a_request_after_close_is_ignored(
        self, store: CacheStore, sample_av: SampleMedia
    ) -> None:
        # 止めた後の依頼で、停止済みの executor へ投げて RuntimeError にならない
        analyzer = MediaAnalyzer(store)
        analyzer.close()
        analyzer.request(probe_media(sample_av.path))
        assert not analyzer._running


class TestAnalyzerAfterForget:
    def test_a_result_that_arrives_after_forget_is_dropped(
        self, store: CacheStore, sample_av: SampleMedia
    ) -> None:
        """解析の途中で素材を外したら、終わっても結果を登録しない

        確かめずに登録すると、外したはずの素材の波形が復活する
        """
        media = probe_media(sample_av.path)
        waveform = analyze_waveform(sample_av.path)
        assert waveform is not None

        analyzer = MediaAnalyzer(store)
        try:
            analyzer._running.add(("waveform", media.id))
            analyzer.forget(media.id)
            assert analyzer._publish("waveform", media.id, waveform) is False
            assert analyzer.waveform(media) is None
        finally:
            analyzer.close()


class TestConcurrentWrites:
    def test_two_writers_to_the_same_place_both_succeed(self, tmp_path: Path) -> None:
        """同じ保存先へ同時に書いても落ちない

        キャッシュのキーは素材のパスから作るので、同じ動画を 2 回読み込むと
        2 本の解析が同じ保存先に着く 一時ファイルの名前が固定だと、片方の
        差し替えがもう片方の書きかけを奪って FileNotFoundError になる
        """
        target = tmp_path / "shared.npz"
        errors: list[BaseException] = []

        def write(value: int) -> None:
            try:
                for _ in range(20):
                    save_arrays(target, {"value": np.array([value])})
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=write, args=(n,)) for n in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert errors == []
        assert load_arrays(target) is not None
        # 書きかけが残っていない
        assert [p.name for p in tmp_path.iterdir()] == ["shared.npz"]


class TestBrokenWaveformCache:
    @pytest.mark.parametrize("missing", ["sample_rate", "channels", "total_samples"])
    def test_missing_metadata_reads_as_absent(
        self, store: CacheStore, sample_av: SampleMedia, missing: str
    ) -> None:
        # 壊れていたら None の約束 例外にすると作り直す機会が無くなる
        waveform = analyze_waveform(sample_av.path)
        assert waveform is not None
        path = save_waveform(store, "鍵", waveform)

        data = load_arrays(path)
        assert data is not None
        del data[missing]
        save_arrays(path, data)

        assert load_waveform(store, "鍵") is None


class _FakeContext:
    def __init__(self, major: int, minor: int) -> None:
        self._format = QSurfaceFormat()
        self._format.setVersion(major, minor)

    def format(self) -> QSurfaceFormat:
        return self._format


class _Holder:
    def __init__(self, major: int, minor: int) -> None:
        self._context = _FakeContext(major, minor)


@pytest.mark.parametrize(("major", "minor"), [(3, 3), (4, 1)])
def test_an_older_gl_is_refused_before_its_functions_are_used(major: int, minor: int) -> None:
    """4.3 未満のコンテキストを通さない

    シェーダや頂点配列の関数は 2.0 / 3.0 から在るので、関数の有無だけを見ると
    3.x でも通ってしまう 4.3 の機能を使う所まで進んでから落ちることになる
    """
    with pytest.raises(GLContextError, match=f"{major}.{minor}"):
        OffscreenGLContext._require_usable_gl(_Holder(major, minor))  # type: ignore[arg-type]
