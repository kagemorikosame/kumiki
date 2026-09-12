"""画面配置とショートカットの保存先

どちらも「その人の使い方」で、プロジェクトではなく本人に付く ``%APPDATA%\\Kumiki``
に置くのはそのため（キャッシュや退避と違い、消えると作り直せない）

Qt の既定の保存先（Windows ではレジストリ）は使わない 自動更新で本体を入れ替えても
残ること、手で消したり他の機械へ持っていったりできることを優先して、ファイルにする
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from PySide6.QtCore import QByteArray, QSettings
from PySide6.QtWidgets import QMainWindow

__all__ = [
    "LAYOUT_VERSION",
    "ShortcutStore",
    "Workspace",
    "config_root",
    "find_conflicts",
]

#: 画面配置の版 パネルを足したり名前を変えたりしたら上げる
#: 古い配置をそのまま当てると、新しいパネルがどこにも出てこない
LAYOUT_VERSION = 1


def config_root() -> Path:
    """本人の設定を置く場所"""
    base = os.environ.get("APPDATA") or os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base) / "Kumiki"
    return Path.home() / ".config" / "kumiki"


class Workspace:
    """ウィンドウの大きさと、パネルの並び"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path if path is not None else config_root() / "workspace.ini"

    def save(self, window: QMainWindow) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        settings = QSettings(str(self.path), QSettings.Format.IniFormat)
        settings.setValue("geometry", window.saveGeometry())
        settings.setValue("state", window.saveState(LAYOUT_VERSION))
        settings.sync()

    def restore(self, window: QMainWindow) -> bool:
        """保存した並びに戻す 無い・版が違うときは何もせず偽を返す"""
        if not self.path.is_file():
            return False
        settings = QSettings(str(self.path), QSettings.Format.IniFormat)
        geometry = settings.value("geometry")
        state = settings.value("state")
        if isinstance(geometry, QByteArray):
            window.restoreGeometry(geometry)
        return isinstance(state, QByteArray) and window.restoreState(state, LAYOUT_VERSION)


class ShortcutStore:
    """既定から変えたショートカットだけを持つ

    全部を書き出さないのは、新しい版で既定を変えたときに、本人が触っていない
    ものまで古い既定のまま固まってしまうため 値が空文字なら「割り当てなし」
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path if path is not None else config_root() / "shortcuts.json"

    def load(self) -> dict[str, str]:
        """壊れていても起動は止めない 既定のまま使えれば困らない"""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(key): value for key, value in data.items() if isinstance(value, str)}

    def save(self, overrides: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".writing")
        temporary.write_text(
            json.dumps(dict(sorted(overrides.items())), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)


def find_conflicts(bindings: dict[str, str]) -> dict[str, list[str]]:
    """同じキーに 2 つ以上の操作が割り当たっているもの キー → 操作の一覧

    Qt は重なったショートカットを、どちらも実行しない（黙って何も起きない）
    押しても反応しないのは壊れたように見えるので、決める時点で止める
    """
    owners: dict[str, list[str]] = {}
    for action, key in bindings.items():
        if key:
            owners.setdefault(key, []).append(action)
    return {key: actions for key, actions in owners.items() if len(actions) > 1}
