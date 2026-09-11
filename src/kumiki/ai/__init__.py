"""AI エージェント連携。

編集操作を MCP のツールとして公開し、Claude Agent SDK 経由で会話しながら
実際の編集をさせる。

層の分け方:

- :mod:`~kumiki.ai.operations` — 何ができるか。Qt も MCP も知らない素の関数
- :mod:`~kumiki.ai.host` — 編集ソフト側に求めること
- :mod:`~kumiki.ai.bridge` — AI スレッドから UI スレッドへの橋渡しと確認
- :mod:`~kumiki.ai.server` — 操作を MCP ツールへ包む
- :mod:`~kumiki.ai.session` — SDK との会話

SDK を import するのは server と session だけ。未導入でも操作の定義は読めるので、
チャットパネルは開けるし、導入ボタンも出せる。
"""

from kumiki.ai.bridge import Approval, EditorBridge
from kumiki.ai.environment import AI_PACK
from kumiki.ai.host import EditorHost, ToolError
from kumiki.ai.operations import OPERATIONS, ImageResult, Operation, find_operation

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
