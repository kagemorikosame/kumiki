"""Claude Agent SDK との会話を、UI から扱える形に包む。

SDK は asyncio で動き、``claude`` コマンドを子プロセスとして起動する。Qt の
イベントループとは混ぜられないので、専用スレッドで asyncio を回し、やり取りは
キュー越しにする（:mod:`novaedit.ai.bridge` と同じ考え方）。

UI がやることは 3 つだけ。:meth:`AgentSession.send` で送り、:meth:`AgentSession.poll`
で溜まった出来事を拾い、:meth:`AgentSession.interrupt` で止める。
"""

from __future__ import annotations

import asyncio
import queue
import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from novaedit.ai.bridge import EditorBridge
from novaedit.ai.environment import find_claude_cli
from novaedit.ai.server import SERVER_NAME, build_server

__all__ = ["SYSTEM_PROMPT", "AgentEvent", "AgentSession", "EventKind"]

#: エージェントへの指示。ツールの意味と、この編集ソフト特有の約束事を伝える。
SYSTEM_PROMPT = """\
あなたは動画編集ソフト NovaEdit の中で動く編集アシスタントです。ユーザーの指示を、
用意されたツールで実際の編集操作に変えてください。

守ること:

- タイムライン上の時刻はすべて**プロジェクト fps 基準の整数フレーム**です。秒で
  言われたら fps を掛けて frame に直します（get_project で fps が分かります）。
- 素材やクリップは ID で指します。まず list_media / list_clips で ID を確かめてから
  操作してください。当てずっぽうの ID は失敗します。
- 何かを変えたら preview_frame でその位置を描いて、**自分の目で結果を確かめて**
  ください。数値が正しくても見た目が意図と違うことがあります。
- 映像と音声はリンクしています。片方を分割・削除・移動・トリムすると、もう片方も
  同じように動きます。**両方に同じ操作をしないでください**（2 回目は失敗します）。
- 字幕は素材に紐付いていて、カットや分割には自動で追従します。字幕の位置を手で
  合わせ直す必要はありません。
- 色は #RRGGBB で指定します。
- 大きく変える前に、何をするつもりかを 1〜2 文で伝えてください。変更系の操作には
  ユーザーの確認が入ります。
- 失敗したら、エラーの文面を読んで直してから再試行してください。同じ操作を
  そのまま繰り返さないこと。

返事は日本語で、簡潔に。作業の実況ではなく、やったことと結果を伝えてください。
"""


class EventKind(Enum):
    READY = "ready"
    TEXT = "text"
    THINKING = "thinking"
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"
    TURN_DONE = "turn_done"
    ERROR = "error"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """エージェントからの 1 件の知らせ。"""

    kind: EventKind
    text: str = ""
    tool: str = ""
    detail: str = ""


class AgentSession:
    """1 つの会話。

    会話は繋ぎっぱなしにする。1 回ごとに繋ぎ直すと、それまでのやり取りを毎回
    渡し直すことになり、待ち時間も費用も増える。
    """

    def __init__(
        self,
        bridge: EditorBridge,
        *,
        model: str | None = None,
        cwd: Path | None = None,
        system_prompt: str = SYSTEM_PROMPT,
    ) -> None:
        self._bridge = bridge
        self._model = model
        self._cwd = cwd
        self._system_prompt = system_prompt

        self._events: queue.Queue[AgentEvent] = queue.Queue()
        self._prompts: queue.Queue[str | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: Any = None
        self._busy = threading.Event()
        self._closed = threading.Event()

    # --- UI スレッドから ---

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def busy(self) -> bool:
        """いま応答を生成している最中か。"""
        return self._busy.is_set()

    def start(self) -> None:
        if self.running:
            return
        self._closed.clear()
        self._thread = threading.Thread(target=self._run, name="novaedit-agent", daemon=True)
        self._thread.start()

    def send(self, prompt: str) -> None:
        """指示を送る。まだ繋がっていなければ繋いでから送る。"""
        self.start()
        self._bridge.resume()
        self._prompts.put(prompt)

    def poll(self) -> list[AgentEvent]:
        """溜まった出来事を取り出す。ブロックしない。"""
        drained: list[AgentEvent] = []
        while True:
            try:
                drained.append(self._events.get_nowait())
            except queue.Empty:
                return drained

    def interrupt(self) -> None:
        """生成を止める。待っているツールも諦めさせる。"""
        self._bridge.cancel()
        loop, client = self._loop, self._client
        if loop is not None and client is not None:
            asyncio.run_coroutine_threadsafe(_safe_interrupt(client), loop)

    def close(self) -> None:
        """会話を畳む。終了時に呼ぶ。"""
        if not self.running:
            return
        self._closed.set()
        self._bridge.cancel()
        self._prompts.put(None)
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5.0)

    # --- エージェントスレッド ---

    def _run(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception as exc:  # スレッドの外へ例外を出さない
            self._emit(AgentEvent(EventKind.ERROR, text=_explain(exc)))
        finally:
            self._loop = None
            self._client = None
            self._busy.clear()
            self._emit(AgentEvent(EventKind.CLOSED))

    async def _main(self) -> None:
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

        self._loop = asyncio.get_running_loop()
        # PATH に無い場所へ入っていることがあるので、見つけた場所を明示的に渡す。
        cli = find_claude_cli()
        options = ClaudeAgentOptions(
            system_prompt=self._system_prompt,
            mcp_servers={SERVER_NAME: build_server(self._bridge)},
            # 組み込みのツール（ファイル読み書き・シェル・検索）は全部止める。
            # 編集の話をしているつもりで、ディスクの中身を読まれては困る。
            tools=[],
            # allowed_tools は使わない。並べると SDK がそちらで先に通してしまい、
            # 下の門番が呼ばれなくなる。許可の判断を 1 か所に寄せる。
            can_use_tool=self._can_use_tool,
            # ソフトの中の会話なので、リポジトリの設定やスキルは読み込まない。
            # ここを開けると、編集と関係ない指示が混ざる。
            setting_sources=[],
            permission_mode="default",
            model=self._model,
            cwd=str(self._cwd) if self._cwd is not None else None,
            cli_path=str(cli) if cli is not None else None,
        )

        async with ClaudeSDKClient(options) as client:
            self._client = client
            self._emit(AgentEvent(EventKind.READY))
            while not self._closed.is_set():
                prompt = await asyncio.to_thread(self._prompts.get)
                if prompt is None:
                    break
                await self._turn(client, prompt)

    async def _turn(self, client: Any, prompt: str) -> None:
        self._busy.set()
        try:
            await client.query(prompt)
            async for message in client.receive_response():
                self._translate(message)
        except Exception as exc:
            self._emit(AgentEvent(EventKind.ERROR, text=_explain(exc)))
        finally:
            self._busy.clear()
            self._emit(AgentEvent(EventKind.TURN_DONE))

    async def _can_use_tool(self, name: str, arguments: dict[str, Any], context: Any) -> Any:
        """この編集ソフトのツール以外は使わせない。

        ファイル操作やシェルまで開けると、編集の話をしているつもりが別のことを
        されうる。変更系の確認はブリッジ側で取っているので、ここは門番だけ。
        """
        del arguments, context
        from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

        if name.startswith(f"mcp__{SERVER_NAME}__"):
            return PermissionResultAllow()
        return PermissionResultDeny(message=f"{name} はこの画面からは使えません")

    def _translate(self, message: Any) -> None:
        """SDK のメッセージを、UI が知っている形へ。"""
        from claude_agent_sdk import (
            AssistantMessage,
            ResultMessage,
            TextBlock,
            ThinkingBlock,
            ToolResultBlock,
            ToolUseBlock,
            UserMessage,
        )

        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    self._emit(AgentEvent(EventKind.TEXT, text=block.text))
                elif isinstance(block, ThinkingBlock):
                    self._emit(AgentEvent(EventKind.THINKING, text=block.thinking))
                elif isinstance(block, ToolUseBlock):
                    self._emit(
                        AgentEvent(
                            EventKind.TOOL_USE,
                            tool=_short_name(block.name),
                            detail=_summarize(block.input),
                        )
                    )
        elif isinstance(message, UserMessage):
            for block in message.content if isinstance(message.content, list) else ():
                if isinstance(block, ToolResultBlock):
                    self._emit(
                        AgentEvent(
                            EventKind.TOOL_RESULT,
                            detail=_summarize(block.content),
                            text="失敗" if block.is_error else "",
                        )
                    )
        elif isinstance(message, ResultMessage) and message.is_error:
            self._emit(AgentEvent(EventKind.ERROR, text=str(message.result or "失敗しました")))

    def _emit(self, event: AgentEvent) -> None:
        self._events.put(event)


async def _safe_interrupt(client: Any) -> None:
    try:
        await client.interrupt()
    except Exception:  # 止める処理で落ちても意味が無い
        return


def _short_name(name: str) -> str:
    prefix = f"mcp__{SERVER_NAME}__"
    return name[len(prefix) :] if name.startswith(prefix) else name


def _summarize(value: object, limit: int = 160) -> str:
    """ツールの引数や結果を 1 行に畳む。"""
    if isinstance(value, dict):
        text = ", ".join(f"{k}={v}" for k, v in value.items())
    elif isinstance(value, list):
        parts = []
        for entry in value:
            if isinstance(entry, dict) and entry.get("type") == "text":
                parts.append(str(entry.get("text", "")))
            elif isinstance(entry, dict) and entry.get("type") == "image":
                parts.append("（画像）")
            else:
                parts.append(str(entry))
        text = " ".join(parts)
    else:
        text = str(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _explain(exc: BaseException) -> str:
    """例外を、次にどうすればよいか分かる文へ。"""
    name = type(exc).__name__
    if name == "CLINotFoundError":
        return (
            "Claude Code が見つかりません。npm install -g @anthropic-ai/claude-code で"
            "入れてから、もう一度お試しください。"
        )
    if name == "ProcessError":
        return f"Claude Code の起動に失敗しました: {exc}"
    return f"{name}: {exc}"
