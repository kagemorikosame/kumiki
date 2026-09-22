"""AI 連携の実行環境が未導入でも、ソフトが起動すること

Claude Agent SDK は同梱していない（計画書 F-9-9） 既定の導入手順
``pip install -e ".[dev]"`` では入らず、ソフト内の導入ボタンから後で入れる

**つまり、配布した先のほとんどの環境では SDK が無い** その状態で
import が通らないと、AI を使う使わない以前にアプリが起動しない

一度そうなっていた ``ai/server.py`` が SDK を表で import していて、
そこへ ``ai/session.py`` → チャットパネル → ``sashimono.ui`` と繋がっていたため、
**UI 全体が読み込めなかった** 手元では SDK を入れてあるので気付かず、
CI（素の環境）で初めて出た
"""

from __future__ import annotations

import builtins
import importlib
import sys
from collections.abc import Iterator
from typing import Any

import pytest

#: 未導入のときに読み込めないと困るもの
#: どれか 1 つでも SDK を表で import すると、アプリが起動しなくなる
MODULES = [
    "sashimono.ai",
    "sashimono.ai.server",
    "sashimono.ai.session",
    "sashimono.ui.chat",
    "sashimono.ui.main_window",
    "sashimono.ui",
    "sashimono.app",
]


@pytest.fixture
def without_sdk() -> Iterator[None]:
    """SDK が入っていない環境を作る

    ``sys.modules`` から消すだけでは足りない 入っていれば ``import`` が
    普通に成功してしまうので、import そのものを止める
    """
    real_import = builtins.__import__

    def blocked(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "claude_agent_sdk" or name.startswith("claude_agent_sdk."):
            raise ModuleNotFoundError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    saved = {
        name: module
        for name, module in list(sys.modules.items())
        if name.startswith(("sashimono.ai", "sashimono.ui", "sashimono.app", "claude_agent_sdk"))
    }
    for name in saved:
        del sys.modules[name]

    builtins.__import__ = blocked
    try:
        yield
    finally:
        builtins.__import__ = real_import
        for name in list(sys.modules):
            if name.startswith(("sashimono.ai", "sashimono.ui", "sashimono.app")):
                del sys.modules[name]
        sys.modules.update(saved)


@pytest.mark.parametrize("module", MODULES)
def test_it_imports_without_the_sdk(module: str, without_sdk: None) -> None:
    """未導入でも読み込める 落ちたらアプリが起動しない"""
    importlib.import_module(module)


def test_the_operations_are_still_listed_without_the_sdk(without_sdk: None) -> None:
    """未導入でも操作の一覧は読める

    チャットパネルは「何ができるか」を出しつつ導入ボタンを見せるので、
    ここが読めないとパネル自体を開けない
    """
    ai = importlib.import_module("sashimono.ai")
    assert len(ai.OPERATIONS) > 0


def test_building_the_server_needs_the_sdk(without_sdk: None) -> None:
    """組み立てようとした時点で初めて足りないと分かる

    黙って空のサーバを返すと、AI が「ツールが無い」と言い出して原因が遠くなる
    """
    server = importlib.import_module("sashimono.ai.server")
    with pytest.raises(ModuleNotFoundError):
        server.build_server(object())
