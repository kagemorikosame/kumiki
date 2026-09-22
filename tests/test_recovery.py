"""保存していない作業の退避と、上書き前のバックアップ

退避は「落ちたときにだけ」拾えなければならない 生きている別の起動の退避を
拾うと、同じ作業が 2 つの窓で別々に進み、どちらかの変更が消える
"""

from __future__ import annotations

from datetime import datetime, tzinfo
from pathlib import Path

import pytest

from sashimono.core.io import (
    RecoverySession,
    backup_before_save,
    backup_folder,
    discard,
    find_orphans,
    load_project,
    recovery,
)
from sashimono.core.model import Project


def crash(session: RecoverySession) -> None:
    """落ちたことにする 退避は消さずに錠だけ手放す（プロセスが消えたときと同じ）

    錠のファイルは残したまま、開いていた手元（Windows では開いたファイル、それ以外では
    ``flock``）だけを閉じる 生死は中身ではなくこの手元で見るので、錠のファイルが
    残っていても「持ち主はもういない」と判定される 落ちたあとの状態そのものになる
    """
    lock = session._lock
    assert lock is not None
    lock.abandon()
    session._lock = None


class TestRecovery:
    def test_a_live_session_is_not_an_orphan(self, tmp_path: Path) -> None:
        session = RecoverySession(tmp_path)
        session.save(Project.create(name="作業中"), None)
        try:
            assert find_orphans(tmp_path) == []
        finally:
            session.close()

    def test_a_crashed_session_is_found(self, tmp_path: Path) -> None:
        session = RecoverySession(tmp_path)
        source = tmp_path / "本編.sme"
        session.save(Project.create(name="本編"), source)
        crash(session)

        entries = find_orphans(tmp_path)
        assert [(entry.name, entry.source) for entry in entries] == [("本編", source)]
        assert load_project(entries[0].path).name == "本編"

    def test_a_clean_exit_leaves_nothing(self, tmp_path: Path) -> None:
        session = RecoverySession(tmp_path)
        session.save(Project.create(), None)
        session.close()
        assert list((tmp_path / "recovery").iterdir()) == []

    def test_clearing_forgets_the_saved_state(self, tmp_path: Path) -> None:
        # 保存した直後に退避が残っていると、次に落ちたとき保存前の古い状態を勧めてしまう
        session = RecoverySession(tmp_path)
        session.save(Project.create(), None)
        session.clear()
        crash(session)
        assert find_orphans(tmp_path) == []

    def test_discard_removes_it(self, tmp_path: Path) -> None:
        session = RecoverySession(tmp_path)
        session.save(Project.create(), None)
        crash(session)
        discard(find_orphans(tmp_path)[0])
        assert find_orphans(tmp_path) == []
        assert list((tmp_path / "recovery").iterdir()) == []

    def test_newest_comes_first(self, tmp_path: Path) -> None:
        older = RecoverySession(tmp_path)
        older.save(Project.create(name="古い"), None)
        crash(older)
        meta = tmp_path / "recovery" / f"{older.session}.json"
        meta.write_text(meta.read_text("utf-8").replace("20", "19", 1), "utf-8")

        newer = RecoverySession(tmp_path)
        newer.save(Project.create(name="新しい"), None)
        crash(newer)
        assert [entry.name for entry in find_orphans(tmp_path)] == ["新しい", "古い"]

    def test_a_broken_note_is_skipped_not_deleted(self, tmp_path: Path) -> None:
        # 読めないからといって消すと、中身の退避まで失う
        session = RecoverySession(tmp_path)
        session.save(Project.create(), None)
        crash(session)
        (tmp_path / "recovery" / f"{session.session}.json").write_text("{", "utf-8")
        assert find_orphans(tmp_path) == []
        assert session.path.exists()

    def test_a_crash_during_the_first_save_is_still_found(self, tmp_path: Path) -> None:
        # 中身を書いた直後、メモを書く前に落ちた形 メモだけを数えていると、
        # 最初の 30 秒ぶんの作業が復元の候補に出ずに消える
        session = RecoverySession(tmp_path)
        session.save(Project.create(name="最初の退避"), None)
        (tmp_path / "recovery" / f"{session.session}.json").unlink()
        crash(session)

        (entry,) = find_orphans(tmp_path)
        assert entry.path == session.path
        assert load_project(entry.path).name == "最初の退避"

    def test_two_sessions_do_not_share_a_file(self, tmp_path: Path) -> None:
        first, second = RecoverySession(tmp_path), RecoverySession(tmp_path)
        try:
            assert first.path != second.path
        finally:
            first.close()
            second.close()


class TestBackup:
    def test_nothing_to_back_up_before_the_first_save(self, tmp_path: Path) -> None:
        # 壊れると、初めての保存が「控えるものが無い」例外で失敗する
        assert backup_before_save(tmp_path / "無い.sme", tmp_path / "state") is None

    def test_the_previous_contents_are_kept(self, tmp_path: Path) -> None:
        # 壊れると、上書きで壊した保存を戻せない（バックアップの意味が無くなる）
        target = tmp_path / "本編.sme"
        target.write_text("前の中身", "utf-8")
        copied = backup_before_save(target, tmp_path / "state")
        assert copied is not None
        assert copied.read_text("utf-8") == "前の中身"

    def test_old_generations_are_pruned(self, tmp_path: Path) -> None:
        # 壊れると、保存するたびに控えが増え続けてディスクを埋める
        target = tmp_path / "本編.sme"
        for index in range(4):
            target.write_text(str(index), "utf-8")
            backup_before_save(target, tmp_path / "state", keep=2)
        kept = sorted(backup_folder(target, tmp_path / "state").iterdir())
        assert [path.read_text("utf-8") for path in kept] == ["2", "3"]

    def test_saves_within_the_same_instant_keep_every_copy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Windows の Python 3.12 は時刻の刻みが約 15ms 続けて保存すると同じ時刻になり、
        # 名前が重なって前の控えを上書きしていた（CI で 3.12 だけ、たまに落ちた）
        frozen = datetime(2026, 9, 12, 12, 0, 0)

        class Stopped(datetime):
            @classmethod
            def now(cls, tz: tzinfo | None = None) -> Stopped:
                del tz
                return cls.fromtimestamp(frozen.timestamp())

        monkeypatch.setattr(recovery, "datetime", Stopped)
        target = tmp_path / "本編.sme"
        for index in range(3):
            target.write_text(str(index), "utf-8")
            backup_before_save(target, tmp_path / "state")
        kept = sorted(backup_folder(target, tmp_path / "state").iterdir())
        assert [path.read_text("utf-8") for path in kept] == ["0", "1", "2"]

    def test_a_name_already_taken_is_never_overwritten(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 別の窓が同じ瞬間に同じ名前を押さえた形 上書きすると、その窓の控えが消える
        class Stopped(datetime):
            @classmethod
            def now(cls, tz: tzinfo | None = None) -> Stopped:
                del tz
                return cls(2026, 9, 12, 12, 0, 0)

        monkeypatch.setattr(recovery, "datetime", Stopped)
        target = tmp_path / "本編.sme"
        target.write_text("こちら", "utf-8")
        folder = backup_folder(target, tmp_path / "state")
        folder.mkdir(parents=True)
        taken = folder / "20260912-120000-000000-000.sme"
        taken.write_text("別の窓", "utf-8")

        copied = backup_before_save(target, tmp_path / "state")
        assert copied is not None and copied != taken
        assert taken.read_text("utf-8") == "別の窓"
        assert copied.read_text("utf-8") == "こちら"

    def test_same_name_in_another_folder_is_kept_apart(self, tmp_path: Path) -> None:
        # 「本編.sme」はどこにでもある 名前だけで分けると別の作品の控えが混ざる
        state = tmp_path / "state"
        assert backup_folder(tmp_path / "a" / "本編.sme", state) != backup_folder(
            tmp_path / "b" / "本編.sme", state
        )

    @pytest.mark.parametrize("name", ["a:b*c?.sme", "con.sme"])
    def test_awkward_names_still_get_a_folder(self, tmp_path: Path, name: str) -> None:
        # Windows で使えない文字や予約名がそのまま残ると、控えのフォルダを作れず控えが取れない
        folder = backup_folder(tmp_path / name, tmp_path / "state")
        folder.mkdir(parents=True)
        assert folder.is_dir()
