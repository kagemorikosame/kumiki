"""AI エージェント連携。

編集操作を MCP のツールとして公開し、Claude Agent SDK 経由で会話しながら
実際の編集をさせる。

層の分け方:

- :mod:`~novaedit.ai.operations` — 何ができるか。Qt も MCP も知らない素の関数
- :mod:`~novaedit.ai.host` — 編集ソフト側に求めること
- :mod:`~novaedit.ai.bridge` — AI スレッドから UI スレッドへの橋渡しと確認
- :mod:`~novaedit.ai.server` — 操作を MCP ツールへ包む
- :mod:`~novaedit.ai.session` — SDK との会話

SDK を import するのは server と session だけ。未導入でも操作の定義は読めるので、
チャットパネルは開けるし、導入ボタンも出せる。
"""

from novaedit.ai.bridge import Approval, EditorBridge
from novaedit.ai.environment import AI_PACK
from novaedit.ai.host import EditorHost, ToolError
from novaedit.ai.operations import OPERATIONS, ImageResult, Operation, find_operation

__all__ = [
    "AI_PACK",
    "OPERATIONS",
    "Approval",
    "EditorBridge",
    "EditorHost",
    "ImageResult",
    "Operation",
    "ToolError",
    "find_operation",
]
