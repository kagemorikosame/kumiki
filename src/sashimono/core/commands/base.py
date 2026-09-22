"""コマンドの基底"""

from __future__ import annotations

from abc import ABC, abstractmethod

from sashimono.core.model import Project

__all__ = ["Command"]


class Command(ABC):
    """プロジェクトを別のプロジェクトへ変換する操作

    実装は純関数でなければならない 同じ入力から常に同じ出力を返し、副作用を
    持たないこと これが守られていれば、Undo は「前のプロジェクトに戻す」だけで済む

    失敗するときは例外を投げる 中途半端な状態のプロジェクトを返してはいけない
    """

    @property
    @abstractmethod
    def label(self) -> str:
        """履歴パネルに出す操作名"""

    @abstractmethod
    def apply(self, project: Project) -> Project:
        """変更後のプロジェクトを返す 引数は変更しない"""
