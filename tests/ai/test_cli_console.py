"""アシスタントの Claude Code（``claude.exe``）を黒い窓なしで起こすこと（#228）

窓を持たない配布版からコンソールのプログラムを起こすと、黒い窓が開く SDK は起こし方を
変える口を持たないので、SDK の transport の部品が見ている ``anyio`` を包む
（:mod:`sashimono.ai.console`） 本物の ``claude.exe`` は起こさず、起こす所を偽物にして
渡された印を見る
"""

from __future__ import annotations

import asyncio
import sys
from types import ModuleType
from typing import Any, cast

import pytest

from sashimono.ai.bridge import EditorBridge
from sashimono.ai.console import CREATE_NO_WINDOW, QuietProcesses, hide_cli_console
from sashimono.ai.session import AgentSession

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="窓の印は Windows だけの話")

sdk = pytest.importorskip("claude_agent_sdk")
subprocess_cli = pytest.importorskip("claude_agent_sdk._internal.transport.subprocess_cli")


@pytest.fixture(autouse=True)
def restore_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    # 包みは同じ Python の中に残る 試験のあとで元へ戻し、ほかの試験の SDK を変えない
    monkeypatch.setattr(subprocess_cli, "anyio", subprocess_cli.anyio)


class _RefusedError(Exception):
    """起こす所の偽物が投げる 本物のプログラムは起こさない"""


def _record_starts(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """本物の ``anyio.open_process`` を、渡された設定を覚えて断る偽物にする"""
    started: list[dict[str, Any]] = []

    async def fake(command: Any, **options: Any) -> Any:
        started.append({"command": command, **options})
        raise _RefusedError

    real = subprocess_cli.anyio
    target = real._real if isinstance(real, QuietProcesses) else real
    monkeypatch.setattr(cast(ModuleType, target), "open_process", fake)
    return started


def test_the_sdk_starts_claude_without_a_window(monkeypatch: pytest.MonkeyPatch) -> None:
    # 印が無いと、配布版でアシスタントを使うたびに claude.exe の黒い窓が開く
    assert hide_cli_console()
    started = _record_starts(monkeypatch)
    transport = subprocess_cli.SubprocessCLITransport(
        prompt="", options=sdk.ClaudeAgentOptions(cli_path="C:/無い/claude.exe")
    )
    # 版を確かめる所は SDK が claude.exe を起こす所の 1 つ 失敗は SDK が握るので例外は出ない
    asyncio.run(transport._check_claude_version())
    (options,) = started
    assert options["command"][0] == "C:/無い/claude.exe"
    assert options["creationflags"] & CREATE_NO_WINDOW


def test_other_parts_of_anyio_are_untouched() -> None:
    # 包みが open_process 以外を隠すと、SDK が使う Lock や取り消しの枠が無くなって繋がらない
    real = subprocess_cli.anyio
    assert hide_cli_console()
    assert subprocess_cli.anyio.Lock is real.Lock
    assert subprocess_cli.anyio.CancelScope is real.CancelScope


def test_wrapping_twice_keeps_one_layer() -> None:
    # 繋ぎ直すたびに包みが重なると、呼ぶたびに深くなる
    assert hide_cli_console()
    once = subprocess_cli.anyio
    assert hide_cli_console()
    assert subprocess_cli.anyio is once


def test_existing_flags_are_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    # 呼び手が渡した印を上書きすると、別の印（新しいプロセスの組など）が黙って消える
    hide_cli_console()
    started = _record_starts(monkeypatch)
    with pytest.raises(_RefusedError):
        asyncio.run(subprocess_cli.anyio.open_process(["x"], creationflags=0x200))
    assert started[0]["creationflags"] == 0x200 | CREATE_NO_WINDOW


class _QuietBridge:
    def resume(self) -> None:
        return

    def cancel(self) -> None:
        return


def test_the_session_wraps_before_connecting(monkeypatch: pytest.MonkeyPatch) -> None:
    # 繋いでから包むと、そのときにはもう claude.exe の窓が開いている
    seen: list[bool] = []

    class _Client:
        def __init__(self, _options: object) -> None:
            seen.append(isinstance(subprocess_cli.anyio, QuietProcesses))

        async def __aenter__(self) -> _Client:
            raise _RefusedError

        async def __aexit__(self, *_exc: object) -> None:
            return

    monkeypatch.setattr(sdk, "ClaudeSDKClient", _Client)
    session = AgentSession(cast(EditorBridge, _QuietBridge()))
    with pytest.raises(_RefusedError):
        asyncio.run(session._main())
    assert seen == [True]
