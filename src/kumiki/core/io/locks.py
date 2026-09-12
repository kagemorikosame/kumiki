"""「いま使っている」を錠のファイルで示す

自動退避（:mod:`kumiki.core.io.recovery`）と、プロジェクトを 2 つの窓で開いたことの
検出が同じ仕組みを使う

生きているかどうかは、開いたままにしているファイルを消せるかで見分ける Windows では
開いているファイルを消せないので、「消せたら持ち主はもういない」と判断できる
プロセス番号で見る方法は Windows では使わない 番号は使い回されるうえ、Windows の
``os.kill`` は存在の確認ではなく強制終了になる
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import IO

__all__ = ["HeldLock", "is_held", "try_hold"]


class HeldLock:
    """開いたままにしている錠 :meth:`release` を呼ぶか、プロセスが終わるまで効く"""

    def __init__(self, path: Path, handle: IO[str]) -> None:
        self.path = path
        self._handle: IO[str] | None = handle
        handle.write(str(os.getpid()))
        handle.flush()

    def release(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None
        self.path.unlink(missing_ok=True)


def is_held(path: Path) -> bool:
    """誰かが持っているか 持ち主が終わっていれば錠を片付けて偽を返す"""
    if not path.exists():
        return False
    if os.name == "nt":
        try:
            path.unlink()
        except PermissionError:
            return True
        except FileNotFoundError:
            return False
        return False
    # Windows 以外では開いていても消せてしまう 番号の使い回しは承知のうえで
    # プロセスの有無で見る（この製品の対象は Windows で、ここは開発用の逃げ道）
    try:
        os.kill(int(path.read_text(encoding="utf-8")), 0)
    except (ProcessLookupError, ValueError, OSError):
        path.unlink(missing_ok=True)
        return False
    return True


def try_hold(path: Path) -> HeldLock | None:
    """空いていれば押さえる 誰かが持っていれば ``None``

    排他作成（``"x"``）で 1 回で押さえる 「空いているか見てから作る」の 2 段だと、
    2 つの窓を同時に開いたときに両方が錠を取れてしまい、知らせる仕組みが働かない
    既にあった錠の持ち主が終わっていれば、:func:`is_held` が片付けるので 1 度だけ
    取り直す
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            handle = path.open("x", encoding="utf-8")
        except FileExistsError:
            if is_held(path):
                return None
            continue
        return HeldLock(path, handle)
    return None
