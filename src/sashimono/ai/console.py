"""Claude Code（``claude.exe``）を起こすときに黒い窓を出さない（#228）

配る本体は窓を持たない（コンソールの無い）プログラムで、そこからコンソールのプログラムを
起こすと、そのための黒い窓が開く Claude Agent SDK は ``anyio.open_process`` で
``claude.exe`` を起こし、``creationflags`` を渡す口（設定の項目や起こし方の差し替え）を
持たない 自前の transport を渡す口はあるが、渡すと SDK が会話の前に整える所
（``can_use_tool`` の繋ぎ方・会話の再開）を通らなくなり、同じ物を写すと SDK の版ごとに崩れる

そこで SDK のファイルは書き換えず、SDK の transport の部品が見ている ``anyio`` だけを、
``open_process`` に ``CREATE_NO_WINDOW`` を足す包みへ差し替える ほかの ``anyio`` の
使い手（同じ Python の中のほかの部品）には効かない SDK の中の作りが変わって差し替え先が
見つからなければ何もしない（窓は出るが会話はできる） 差し替えたかは戻り値で分かる
"""

from __future__ import annotations

import importlib
import subprocess
from types import ModuleType
from typing import Any

__all__ = ["CREATE_NO_WINDOW", "QuietProcesses", "hide_cli_console"]

#: 起こしたプログラムに窓を持たせない印 Windows の外では 0（何もしない）
CREATE_NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)

#: SDK が ``claude.exe`` を起こす部品
_TRANSPORT_MODULE = "claude_agent_sdk._internal.transport.subprocess_cli"


class QuietProcesses:
    """``anyio`` の代わりに置く包み ``open_process`` だけ窓を隠し、ほかはそのまま渡す"""

    def __init__(self, real: ModuleType) -> None:
        self._real = real

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    async def open_process(self, command: Any, **options: Any) -> Any:
        # 呼び手の印は残して足す 上書きすると、別の印（新しいプロセスの組など）が消える
        options["creationflags"] = int(options.get("creationflags", 0)) | CREATE_NO_WINDOW
        return await self._real.open_process(command, **options)


def hide_cli_console() -> bool:
    """SDK が起こす ``claude.exe`` に窓を出させない 差し替えた（もう差し替えてある）なら真

    何度呼んでもよい 包みを 2 重にしない Windows の外では何もしない
    """
    if not CREATE_NO_WINDOW:
        return False
    try:
        transport = importlib.import_module(_TRANSPORT_MODULE)
    except ImportError:
        return False
    current = getattr(transport, "anyio", None)
    if isinstance(current, QuietProcesses):
        return True
    if not isinstance(current, ModuleType) or not hasattr(current, "open_process"):
        return False
    transport.anyio = QuietProcesses(current)  # type: ignore[attr-defined]  # 実行時に差し替える
    return True
