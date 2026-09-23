"""素材を作る取り付け口が、使えない環境で「エラー」ではなく「飛ばす」になること

ffmpeg はあるが libx264 が入っていない組み立て方があり、そこでは素材を作る所で
`CalledProcessError` が出ていた 取り付け口の中で落ちるので、試験は skip ではなく
error として並ぶ 他の使えない環境（ffmpeg が無い・GPU が無い）は飛ばす決まりなので、
ここだけ食い違っていた
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.media_fixtures import ENCODER_LIST_TIMEOUT, libx264_available, make_sample

WITH_X264 = """Encoders:
 V..... libx264              libx264 H.264 / AVC / MPEG-4 AVC
 A..... aac                  AAC (Advanced Audio Coding)
"""

#: 名前の欄に libx264 は無く、説明の欄にだけ出る形 ffmpeg の一覧には
#: 「libx264 を使う」と説明に書く別の符号化器が実際に並ぶ
ONLY_IN_THE_DESCRIPTION = """Encoders:
 V..... h264_qsv             H.264 (Intel Quick Sync, libx264 の代わり)
 A..... aac                  AAC (Advanced Audio Coding)
"""


@pytest.fixture(autouse=True)
def forget_the_answer() -> Iterator[None]:
    """覚えた判定を試験ごとに捨てる 覚えたままだと隣の試験へ漏れる"""
    libx264_available.cache_clear()
    yield
    libx264_available.cache_clear()


def fake_ffmpeg(
    monkeypatch: pytest.MonkeyPatch,
    *,
    listing: str = "",
    code: int = 0,
    on_stderr: bool = False,
    hangs: bool = False,
) -> list[dict[str, Any]]:
    """符号化器の一覧を返す偽の ffmpeg を置き、呼ばれた命令と引数を記録する"""
    calls: list[dict[str, Any]] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append({"command": command, **kwargs})
        if hangs:
            raise subprocess.TimeoutExpired(command, kwargs.get("timeout", 0))
        if on_stderr:
            return subprocess.CompletedProcess(command, code, "", listing)
        return subprocess.CompletedProcess(command, code, listing, "")

    monkeypatch.setattr(shutil, "which", lambda _name: "ffmpeg")
    monkeypatch.setattr(subprocess, "run", run)
    return calls


class TestSeeingWhetherH264CanBeBurned:
    def test_an_encoder_list_without_libx264_is_a_no(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """libx264 の無い ffmpeg を「焼ける」と見ると、素材を作る所で落ちる"""
        fake_ffmpeg(monkeypatch, listing="Encoders:\n V..... mpeg4   MPEG-4 part 2\n")
        assert libx264_available() is False

    def test_libx264_only_in_the_description_is_a_no(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """一覧の丸ごとから字を探すと、説明に出るだけの行を数えて取り違える"""
        fake_ffmpeg(monkeypatch, listing=ONLY_IN_THE_DESCRIPTION)
        assert libx264_available() is False

    def test_the_list_failing_is_a_no_not_a_crash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """一覧が取れないときに落ちると、試験全体が動かなくなる"""
        fake_ffmpeg(monkeypatch, listing=WITH_X264, code=1)
        assert libx264_available() is False

    def test_ffmpeg_not_on_the_path_is_a_no(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ffmpeg が無いのに呼ぶと ``FileNotFoundError`` で落ちる"""
        monkeypatch.setattr(shutil, "which", lambda _name: None)
        assert libx264_available() is False

    def test_a_listing_on_the_error_side_still_counts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """一覧を標準エラーへ出す組み立て方でも、焼けるのに飛ばしてしまわない"""
        fake_ffmpeg(monkeypatch, listing=WITH_X264, on_stderr=True)
        assert libx264_available() is True

    def test_an_ffmpeg_that_never_answers_does_not_stop_the_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """待ち時間を切らないと、応答しない ffmpeg でテストが止まったまま戻らない"""
        calls = fake_ffmpeg(monkeypatch, hangs=True)
        assert libx264_available() is False
        assert calls[0]["timeout"] == ENCODER_LIST_TIMEOUT

    def test_the_list_is_read_only_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """毎回 ffmpeg を起こすと、素材を作るたびに 100ms 前後を捨てる"""
        calls = fake_ffmpeg(monkeypatch, listing=WITH_X264)
        assert libx264_available() is True
        assert libx264_available() is True
        assert len(calls) == 1


class TestMakingASample:
    def test_without_libx264_the_test_is_skipped_not_errored(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """飛ばさないと ``CalledProcessError`` が取り付け口のエラーとして並ぶ"""
        fake_ffmpeg(monkeypatch, listing=ONLY_IN_THE_DESCRIPTION)
        with pytest.raises(pytest.skip.Exception):
            make_sample(tmp_path, "av.mp4")

    def test_an_existing_file_is_returned_without_looking(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """すでに作った素材まで飛ばすと、同じ場面を 2 度測れなくなる"""
        (tmp_path / "av.mp4").write_bytes(b"")
        fake_ffmpeg(monkeypatch, listing=ONLY_IN_THE_DESCRIPTION)
        assert make_sample(tmp_path, "av.mp4").path == tmp_path / "av.mp4"
