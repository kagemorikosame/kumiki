"""錠のファイルと、プロジェクトを 2 つの窓で開いたことの検出

錠を取り違えると、2 つの窓で同じファイルを開いても何も言われず、あとから
保存した方の内容だけが黙って残る 逆に、持ち主が終わった錠を「使用中」と見ると、
落ちたあとに開き直すたびに「別の窓で開いています」が出る
"""

from __future__ import annotations

import threading
from pathlib import Path

from kumiki.core.io import (
    HeldLock,
    hold_new,
    is_held,
    others_holding,
    project_presence_dir,
    try_hold,
)


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

    def test_only_one_of_simultaneous_takers_wins(self, tmp_path: Path) -> None:
        # 「空いているか見てから作る」だと、2 つの窓を同時に開いたとき両方が取れる
        # そうなると、どちらにも「別の窓で開いています」が出ない
        path = tmp_path / "a.lock"
        barrier = threading.Barrier(8)
        results: list[HeldLock | None] = []

        def take() -> None:
            barrier.wait()
            results.append(try_hold(path))

        threads = [threading.Thread(target=take) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        held = [lock for lock in results if lock is not None]
        try:
            assert len(held) == 1
        finally:
            for lock in held:
                lock.release()

    def test_a_lock_with_nothing_written_yet_is_still_held(self, tmp_path: Path) -> None:
        # 作った直後の空の錠を「中身が読めない = 持ち主がいない」と取ると、作った
        # ばかりの錠を消して、2 つ目の窓も錠を取れてしまう（中身は見ないこと）
        path = tmp_path / "a.lock"
        lock = try_hold(path)
        assert lock is not None
        try:
            assert path.read_text(encoding="utf-8") == ""
            assert is_held(path)
            assert try_hold(path) is None
            assert path.exists()
        finally:
            lock.release()

    def test_a_dead_owner_can_be_replaced(self, tmp_path: Path) -> None:
        # 落ちた窓の錠が残っていても、次に開いた窓は錠を取れる
        (tmp_path / "a.lock").write_text("終了済み", encoding="utf-8")
        lock = try_hold(tmp_path / "a.lock")
        assert lock is not None
        lock.release()

    def test_a_leftover_from_a_dead_owner_is_not_held(self, tmp_path: Path) -> None:
        # 落ちた窓の錠が残っていても、開き直すたびに警告が出ないこと
        (tmp_path / "a.lock").write_text("終了済み", encoding="utf-8")
        assert not is_held(tmp_path / "a.lock")


class TestPresence:
    def test_each_window_is_counted(self, tmp_path: Path) -> None:
        # 1 つの錠を取り合う形だと、「それでも開く」の窓が数に入らず、先の窓が
        # 閉じたあとに 3 つ目の窓が警告なしで開ける（PR #13） 窓ごとに数える
        folder = tmp_path / "open"
        first, second = hold_new(folder), hold_new(folder)
        try:
            assert others_holding(folder, second.path)
            first.release()
            assert not others_holding(folder, second.path)
            assert others_holding(folder)
        finally:
            second.release()
        assert not others_holding(folder)

    def test_a_crashed_window_is_not_counted(self, tmp_path: Path) -> None:
        # 落ちた窓の錠を数えると、開き直すたびに「別の窓で開いています」が出る
        # 本当に 2 つ開いたときの警告まで読み飛ばされるようになる
        folder = tmp_path / "open"
        folder.mkdir()
        (folder / "dead.lock").write_text("", encoding="utf-8")
        assert not others_holding(folder)


class TestProjectPresenceDir:
    def test_the_same_file_gets_the_same_place(self, tmp_path: Path) -> None:
        # 書き方の違い（.. を挟むなど）で別の場所になると、2 つの窓に気付けない
        direct = project_presence_dir(tmp_path / "本編.kmk", tmp_path / "state")
        roundabout = project_presence_dir(tmp_path / "x" / ".." / "本編.kmk", tmp_path / "state")
        assert direct == roundabout

    def test_other_files_get_other_places(self, tmp_path: Path) -> None:
        # 名前だけで分けると、別のフォルダの「本編.kmk」を開いただけで警告が出る
        state = tmp_path / "state"
        assert project_presence_dir(tmp_path / "a" / "本編.kmk", state) != project_presence_dir(
            tmp_path / "b" / "本編.kmk", state
        )
