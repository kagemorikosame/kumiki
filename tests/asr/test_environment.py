"""起こしの実行環境の検出と導入。

実際に 2 GB を落とすわけにはいかないので、コマンドの組み立てと、子プロセスの
出力を 1 行ずつ拾えることを確かめる。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from novaedit.asr import environment
from novaedit.asr.environment import (
    CUDA_PACKAGES,
    REQUIRED_PACKAGES,
    PackageStatus,
    RuntimeStatus,
    install_command,
    install_runtime,
    runtime_status,
)


def _status(*, installed: bool, cuda: bool) -> RuntimeStatus:
    return RuntimeStatus(
        packages=tuple(
            PackageStatus(name, "1.0" if installed else None) for name in REQUIRED_PACKAGES
        ),
        cuda=tuple(PackageStatus(name, "1.0" if cuda else None) for name in CUDA_PACKAGES),
    )


class TestRuntimeStatus:
    def test_missing_packages_are_reported(self) -> None:
        status = _status(installed=False, cuda=False)
        assert status.ready is False
        assert status.missing(cuda=False) == REQUIRED_PACKAGES

    def test_cpu_only_is_ready_but_not_cuda_ready(self) -> None:
        status = _status(installed=True, cuda=False)
        assert (status.ready, status.cuda_ready) == (True, False)
        assert status.missing(cuda=True) == CUDA_PACKAGES
        assert status.missing(cuda=False) == ()

    def test_summary_distinguishes_the_three_states(self) -> None:
        assert "未導入" in _status(installed=False, cuda=False).summary()
        assert "CPU" in _status(installed=True, cuda=False).summary()
        assert "GPU" in _status(installed=True, cuda=True).summary()

    def test_real_lookup_does_not_raise(self) -> None:
        # 入っていない環境で落ちないことが要点。既定では未導入で配布する。
        assert isinstance(runtime_status().ready, bool)


class TestInstallCommand:
    def test_cuda_packages_are_included_only_when_asked(self) -> None:
        with_cuda = install_command(cuda=True)
        without = install_command(cuda=False)
        assert any("cudnn" in part for part in with_cuda)
        assert not any("cudnn" in part for part in without)

    def test_it_runs_pip_from_the_current_interpreter(self) -> None:
        command = install_command(cuda=False)
        assert command[:4] == [sys.executable, "-m", "pip", "install"]

    def test_faster_whisper_carries_a_lower_bound(self) -> None:
        assert "faster-whisper>=1.1" in install_command(cuda=False)

    def test_frozen_builds_install_into_their_own_folder(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # 固めた実行ファイルには書き込めないので、専用フォルダへ入れる。
        monkeypatch.setattr(environment, "runtime_target_dir", lambda: tmp_path / "runtime")
        command = install_command(cuda=False)
        assert "--target" in command
        assert str(tmp_path / "runtime") in command


class TestInstallRuntime:
    def test_output_arrives_line_by_line(self) -> None:
        lines: list[str] = []
        code = install_runtime(
            command=[sys.executable, "-c", "print('1 行目'); print('2 行目')"],
            on_output=lines.append,
        )
        assert code == 0
        # 1 行目は実行するコマンドそのもの。何が走るのか見せてから始める。
        assert lines[0].startswith("> ")
        assert lines[1:] == ["1 行目", "2 行目"]

    def test_failure_is_reported_as_a_non_zero_code(self) -> None:
        assert install_runtime(command=[sys.executable, "-c", "raise SystemExit(3)"]) == 3

    def test_a_missing_executable_does_not_raise(self) -> None:
        lines: list[str] = []
        code = install_runtime(command=["novaedit-存在しないコマンド"], on_output=lines.append)
        assert code != 0
        assert any("起動できない" in line for line in lines)


class TestActivateRuntime:
    def test_normal_runs_do_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(environment, "runtime_target_dir", lambda: None)
        assert environment.activate_runtime() is None

    def test_the_runtime_folder_is_put_on_the_import_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        target = tmp_path / "runtime"
        target.mkdir()
        monkeypatch.setattr(environment, "runtime_target_dir", lambda: target)
        monkeypatch.setattr(sys, "path", list(sys.path))

        assert environment.activate_runtime() == target
        assert sys.path[0] == str(target)
