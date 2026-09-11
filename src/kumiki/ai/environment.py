"""AI 連携の実行環境

字幕起こしと同じく、既定では入っていない（:mod:`kumiki.runtime` を参照）
Agent SDK 自体は小さいが、実行には **Claude Code 本体**（Node 製の ``claude``
コマンド）が要る これは pip では入らないので、見つからないときは案内だけ出す
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from kumiki.runtime import FeaturePack, PackStatus
from kumiki.runtime import install_command as _install_command

__all__ = [
    "AI_PACK",
    "REQUIRED_PACKAGES",
    "find_claude_cli",
    "install_command",
    "runtime_status",
]

REQUIRED_PACKAGES: tuple[str, ...] = ("claude-agent-sdk>=0.2",)


def find_claude_cli() -> Path | None:
    """``claude`` の実行ファイルを探す

    PATH だけを見ると取りこぼす Claude Code の公式インストーラは
    ``~/.local/bin`` へ置くが、そこを PATH に足すのは shell の設定であって、
    エクスプローラから起動した GUI アプリには引き継がれないことがある
    「入れてあるのに見つからない」が一番分かりにくい失敗なので、既知の場所も見る
    """
    found = shutil.which("claude")
    if found:
        return Path(found)

    candidates = [Path.home() / ".local" / "bin" / name for name in ("claude.exe", "claude")]
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.extend(Path(appdata) / "npm" / name for name in ("claude.cmd", "claude.exe"))
    return next((path for path in candidates if path.exists()), None)


AI_PACK = FeaturePack(
    key="ai",
    label="AI 連携",
    required=REQUIRED_PACKAGES,
    size_mb=40,
    commands=("claude",),
    command_hint="npm install -g @anthropic-ai/claude-code で入れられます",
    locate=lambda name: find_claude_cli() if name == "claude" else shutil.which(name),
)


def runtime_status() -> PackStatus:
    return AI_PACK.status()


def install_command(*, upgrade: bool = False) -> list[str]:
    return _install_command(AI_PACK, extra=False, upgrade=upgrade)
