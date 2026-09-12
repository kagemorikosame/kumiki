"""錠のファイルと、プロジェクトを 2 つの窓で開いたことの検出

錠を取り違えると、2 つの窓で同じファイルを開いても何も言われず、あとから
保存した方の内容だけが黙って残る 逆に、持ち主が終わった錠を「使用中」と見ると、
落ちたあとに開き直すたびに「別の窓で開いています」が出る
"""

from __future__ import annotations

from pathlib import Path

from kumiki.core.io import is_held, project_lock_path, try_hold


class TestLock:
    def test_a_second_holder_is_refused(self, tmp_path: Path) -> None:
        first = try_hold(tmp_path / "a.lock")
        assert first is not None
        try:
            assert try_hold(tmp_path / "a.lock") is None
        finally:
            first.release()

    def test_a_released_lock_is_free_again(self, tmp_path: Path) -> None:
        lock = try_hold(tmp_path / "a.lock")
        assert lock is not None
        lock.release()
        assert not is_held(tmp_path / "a.lock")
        again = try_hold(tmp_path / "a.lock")
        assert again is not None
        again.release()

    def test_a_leftover_from_a_dead_owner_is_not_held(self, tmp_path: Path) -> None:
        # 落ちた窓の錠が残っていても、開き直すたびに警告が出ないこと
        (tmp_path / "a.lock").write_text("終了済み", encoding="utf-8")
        assert not is_held(tmp_path / "a.lock")


class TestProjectLockPath:
    def test_the_same_file_gets_the_same_lock(self, tmp_path: Path) -> None:
        # 書き方の違い（.. を挟むなど）で別の錠になると、2 つの窓に気付けない
        direct = project_lock_path(tmp_path / "本編.kmk", tmp_path / "state")
        roundabout = project_lock_path(tmp_path / "x" / ".." / "本編.kmk", tmp_path / "state")
        assert direct == roundabout

    def test_other_files_get_other_locks(self, tmp_path: Path) -> None:
        state = tmp_path / "state"
        assert project_lock_path(tmp_path / "a" / "本編.kmk", state) != project_lock_path(
            tmp_path / "b" / "本編.kmk", state
        )
