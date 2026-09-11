"""編集操作を MCP のツールとして公開する。

外部プロセスではなく**アプリ内の MCP サーバ**にしてある。編集中のプロジェクトは
メモリ上にしかないので、プロセスをまたぐとファイル経由で受け渡すことになり、
保存していない状態を AI が触れなくなる。

:mod:`kumiki.ai.operations` に並べた操作を、ここで 1 つずつ MCP ツールへ包む。
包む処理は全部同じなので、ツールを足すときに触るのは operations だけで済む。
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

from claude_agent_sdk import SdkMcpTool, create_sdk_mcp_server
from claude_agent_sdk.types import McpSdkServerConfig

from kumiki.ai.bridge import EditorBridge
from kumiki.ai.host import ToolError
from kumiki.ai.operations import OPERATIONS, ImageResult, Operation

__all__ = ["SERVER_NAME", "build_server", "build_tools", "tool_name", "tool_names"]

#: MCP サーバの名前。Claude 側でのツール名は ``mcp__kumiki__<ツール名>`` になる。
SERVER_NAME = "kumiki"


def tool_name(operation: Operation | str) -> str:
    """Claude から見えるツール名。"""
    name = operation if isinstance(operation, str) else operation.name
    return f"mcp__{SERVER_NAME}__{name}"


def tool_names(*, writes: bool | None = None) -> list[str]:
    """ツール名の一覧。``writes`` を指定すると読み取り／変更で絞る。"""
    return [
        tool_name(operation)
        for operation in OPERATIONS
        if writes is None or operation.writes is writes
    ]


def build_tools(bridge: EditorBridge) -> list[SdkMcpTool[Any]]:
    """ブリッジに繋がった MCP ツールを組み立てる。

    サーバとは別に取り出せるようにしてある。``create_sdk_mcp_server`` が返す
    設定からはツールを覗けないので、試験からはこちらを使う。
    """
    return [_wrap(operation, bridge) for operation in OPERATIONS]


def build_server(bridge: EditorBridge) -> McpSdkServerConfig:
    """ブリッジに繋がった MCP サーバを作る。"""
    return create_sdk_mcp_server(name=SERVER_NAME, version="1.0.0", tools=build_tools(bridge))


def _wrap(operation: Operation, bridge: EditorBridge) -> SdkMcpTool[Any]:
    """1 つの操作を MCP ツールにする。"""

    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        try:
            # ブリッジの中で UI スレッドの返事を待つ。ここで直接待つと、
            # SDK のメッセージ受信まで止まって中断が効かなくなる。
            result = await loop.run_in_executor(
                None, lambda: bridge.invoke(operation, dict(arguments))
            )
        except ToolError as exc:
            return _error(str(exc))
        except (ValueError, KeyError) as exc:
            # 編集コマンドが弾いた場合。理由はそのまま返す方が次の手を選びやすい。
            return _error(str(exc))
        return _content(result)

    return SdkMcpTool(
        name=operation.name,
        description=operation.description,
        input_schema=operation.schema,
        handler=handler,
    )


def _content(result: object) -> dict[str, Any]:
    if isinstance(result, ImageResult):
        blocks: list[dict[str, Any]] = [
            {
                "type": "image",
                "data": base64.b64encode(result.png).decode("ascii"),
                "mimeType": "image/png",
            }
        ]
        if result.caption:
            blocks.append({"type": "text", "text": result.caption})
        return {"content": blocks}

    if isinstance(result, str):
        text = result
    else:
        text = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    return {"content": [{"type": "text", "text": text}]}


def _error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "is_error": True}
