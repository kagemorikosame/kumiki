"""保存していない作業の退避と、保存したファイルの世代バックアップ

2 つは守るものが違う

- **退避** 保存していない変更 落ちたときに、次の起動で拾い直す
- **バックアップ** 上書き保存で消える前の中身 「さっきの保存で壊した」を戻す

どちらもプロジェクトの隣ではなく ``%LOCALAPPDATA%\\Kumiki`` に置く 隣に置くと、
プロジェクトを同期フォルダ（OneDrive など）に置いている人のところで、数十秒おきの
退避がそのまま同期されて回線と相手のフォルダを埋める

Qt を使わない 退避の判断はテストで直接確かめたいので、ここは素の Python で書く
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import IO

from kumiki.core.io.serialize import SUFFIX, save_project
from kumiki.core.model import Project

__all__ = [
    "BACKUP_GENERATIONS",
    "RecoveryEntry",
    "RecoverySession",
    "backup_before_save",
    "backup_folder",
    "default_state_root",
    "discard",
    "find_orphans",
]

#: 1 つのプロジェクトについて残すバックアップの数
#: 保存は頻繁に押すものなので、少ないとすぐ押し流される 1 本は数百 KB 程度
BACKUP_GENERATIONS = 20


def default_state_root() -> Path:
    """退避とバックアップを置く既定の場所

    キャッシュ（``cache``）と同じ ``%LOCALAPPDATA%\\Kumiki`` の下だが、別のフォルダに
    分ける キャッシュは消してよいものとして案内するので、同じ所にあると一緒に消される
    """
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_STATE_HOME")
    if base:
        return Path(base) / "Kumiki"
    return Path.home() / ".local" / "state" / "kumiki"


@dataclass(frozen=True, slots=True)
class RecoveryEntry:
    """前回のどこかの起動が残していった退避 1 件

    メモ（``.json``）が無いときは ``name`` が「無題」、``saved_at`` がファイルの
    更新時刻になる 最初の退避の途中で落ちると、中身だけ書けてメモが無い
    """

    session: str
    path: Path
    #: 退避する前に開いていたファイル 1 度も保存していなければ ``None``
    source: Path | None
    name: str
    saved_at: datetime


class RecoverySession:
    """この起動の退避先

    起動ごとに別の名前を使う 同時に 2 つ開いたときに、互いの退避を上書きしない

    生きているかどうかは、開いたままにしている錠のファイルで見分ける Windows では
    開いているファイルを消せないので、「消せたら持ち主はもういない」と判断できる
    プロセス番号で見る方法は使わない 番号は使い回されるうえ、Windows の
    ``os.kill`` は存在の確認ではなく強制終了になる
    """

    def __init__(self, root: Path | None = None) -> None:
        self._folder = (root if root is not None else default_state_root()) / "recovery"
        self._folder.mkdir(parents=True, exist_ok=True)
        self.session = uuid.uuid4().hex
        self._lock: IO[str] | None = self._lock_path(self._folder, self.session).open(
            "w", encoding="utf-8"
        )
        self._lock.write(str(os.getpid()))
        self._lock.flush()

    @property
    def path(self) -> Path:
        return self._folder / f"{self.session}{SUFFIX}"

    def save(self, project: Project, source: Path | None) -> None:
        """いまの状態を退避する 書き込み中に落ちても前回の退避は残る"""
        save_project(project, self.path)
        meta = {
            "source": str(source) if source is not None else None,
            "name": project.name,
            "saved_at": datetime.now().isoformat(timespec="seconds"),
        }
        _write_atomic(self._meta_path(self._folder, self.session), json.dumps(meta))

    def clear(self) -> None:
        """退避を消す 保存した直後など、守るものが無くなったときに呼ぶ"""
        for path in (self.path, self._meta_path(self._folder, self.session)):
            path.unlink(missing_ok=True)

    def close(self) -> None:
        """正常に終わる 退避も錠も残さない"""
        self.clear()
        if self._lock is not None:
            self._lock.close()
            self._lock = None
        self._lock_path(self._folder, self.session).unlink(missing_ok=True)

    @staticmethod
    def _lock_path(folder: Path, session: str) -> Path:
        return folder / f"{session}.lock"

    @staticmethod
    def _meta_path(folder: Path, session: str) -> Path:
        return folder / f"{session}.json"


def find_orphans(root: Path | None = None) -> list[RecoveryEntry]:
    """持ち主が終わっているのに残っている退避 新しい順

    正常に終わった起動は退避を消していくので、ここに出てくるのは落ちたか
    強制終了されたものだけ 読めない退避は黙って飛ばす（消しはしない）
    """
    folder = (root if root is not None else default_state_root()) / "recovery"
    if not folder.is_dir():
        return []

    # メモと中身のどちらか一方しか無いものも拾う 中身から先に書くので、最初の
    # 退避の途中で落ちると中身だけが残る メモだけを数えるとそれを見落とす
    sessions = {path.stem for path in folder.glob("*.json")} | {
        path.name.removesuffix(SUFFIX) for path in folder.glob(f"*{SUFFIX}")
    }
    found: list[RecoveryEntry] = []
    for session in sessions:
        if _is_alive(folder, session):
            continue
        meta_path = folder / f"{session}.json"
        project_path = folder / f"{session}{SUFFIX}"
        if not project_path.is_file():
            meta_path.unlink(missing_ok=True)
            continue
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                saved_at = datetime.fromisoformat(str(meta["saved_at"]))
            except (OSError, ValueError, KeyError, TypeError):
                continue
        else:
            meta = {}
            saved_at = datetime.fromtimestamp(project_path.stat().st_mtime)
        source = meta.get("source")
        found.append(
            RecoveryEntry(
                session=session,
                path=project_path,
                source=Path(source) if isinstance(source, str) else None,
                name=str(meta.get("name") or "無題"),
                saved_at=saved_at,
            )
        )
    return sorted(found, key=lambda entry: entry.saved_at, reverse=True)


def discard(entry: RecoveryEntry) -> None:
    """退避を捨てる 復元し終えたときと、要らないと言われたときに呼ぶ"""
    folder = entry.path.parent
    for path in (
        entry.path,
        folder / f"{entry.session}.json",
        folder / f"{entry.session}.lock",
    ):
        path.unlink(missing_ok=True)


def _is_alive(folder: Path, session: str) -> bool:
    lock = folder / f"{session}.lock"
    if not lock.exists():
        return False
    if os.name == "nt":
        try:
            lock.unlink()
        except PermissionError:
            return True
        except FileNotFoundError:
            return False
        return False
    # Windows 以外では開いていても消せてしまう 番号の使い回しは承知のうえで
    # プロセスの有無で見る（この製品の対象は Windows で、ここは開発用の逃げ道）
    try:
        os.kill(int(lock.read_text(encoding="utf-8")), 0)
    except (ProcessLookupError, ValueError, OSError):
        return False
    return True


def _write_atomic(path: Path, text: str) -> None:
    temporary = path.with_name(path.name + ".writing")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


# --- 世代バックアップ ------------------------------------------------------


def backup_folder(target: Path, root: Path | None = None) -> Path:
    """そのプロジェクトのバックアップを置く場所

    ファイル名だけで分けると、別のフォルダにある同じ名前のプロジェクト
    （「本編.kmk」はどこにでもある）が 1 つの棚に混ざる 場所の要約を添える
    """
    base = (root if root is not None else default_state_root()) / "backups"
    resolved = os.path.normcase(str(Path(target).resolve()))
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:10]
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", Path(target).stem)[:40] or "project"
    return base / f"{stem}-{digest}"


def backup_before_save(
    target: Path, root: Path | None = None, *, keep: int = BACKUP_GENERATIONS
) -> Path | None:
    """上書きする前の中身を控える ``target`` がまだ無ければ何もしない

    古いものから消して ``keep`` 本に保つ 名前に時刻を入れてあるので、並べれば
    そのまま古い順になる
    """
    target = Path(target)
    if not target.is_file():
        return None
    folder = backup_folder(target, root)
    folder.mkdir(parents=True, exist_ok=True)
    copied = folder / f"{datetime.now():%Y%m%d-%H%M%S-%f}{SUFFIX}"
    shutil.copy2(target, copied)

    generations = sorted(folder.glob(f"*{SUFFIX}"))
    for old in generations[: max(0, len(generations) - keep)]:
        old.unlink(missing_ok=True)
    return copied
