"""改名前の置き場（%APPDATA%\\Kumiki など）を、新しい置き場へ引き継ぐ

旧名を残す: このファイル全体（名前の一括置換でも書き換えない 旧名の置き場そのものを試す）

ここが落ちると、改名後の版を初めて起動した人の設定・プリセット・自分で置いた
スクリプト・退避が見えなくなり、既定の状態から始まる（旧い置き場に残ってはいる）
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from sashimono.compat.aviutl.catalog import default_script_roots
from sashimono.core import userdirs
from sashimono.core.io import find_orphans, load_project, project_to_dict
from sashimono.core.io.presets import PresetStore
from sashimono.core.model import Project
from sashimono.core.userdirs import migrate_legacy_folders


@pytest.fixture
def roaming() -> Path:
    # conftest が APPDATA と LOCALAPPDATA をテストごとの一時フォルダへ向けている
    return Path(os.environ["APPDATA"])


@pytest.fixture
def local() -> Path:
    return Path(os.environ["LOCALAPPDATA"])


def _put(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, "utf-8")
    return path


class TestSettings:
    def test_settings_are_copied_and_the_old_folder_stays(self, roaming: Path) -> None:
        # 写すのは、途中で落ちても旧版へ戻しても設定を失わないため
        _put(roaming / "Kumiki" / "shortcuts.json", '{"再生": "K"}')
        _put(roaming / "Kumiki" / "scripts" / "自作" / "揺れ.anm2", "--track0:強さ")

        (note,) = [n for n in migrate_legacy_folders() if n.target == roaming / "Sashimono"]
        assert note.action == "copied"
        assert (roaming / "Sashimono" / "shortcuts.json").read_text("utf-8") == '{"再生": "K"}'
        assert (roaming / "Kumiki" / "shortcuts.json").exists()

    def test_scripts_placed_by_hand_are_found_under_the_new_name(self, roaming: Path) -> None:
        # 自分で置いたスクリプトが見つからないと、使っていた効果が全部「不明」になる
        _put(roaming / "Kumiki" / "scripts" / "揺れ.anm2", "--track0:強さ")
        migrate_legacy_folders()
        script_root = roaming / "Sashimono" / "scripts"
        assert script_root in default_script_roots()
        assert (script_root / "揺れ.anm2").is_file()

    def test_old_presets_appear_in_the_list(self, roaming: Path) -> None:
        _put(
            roaming / "Kumiki" / "presets" / "ユーザー" / "影.kmkp",
            '{"format": "kumiki-preset", "version": 1, "name": "影", "effects": []}',
        )
        migrate_legacy_folders()
        assert [preset.name for preset in PresetStore().all()] == ["影"]

    def test_an_existing_new_folder_is_never_overwritten(self, roaming: Path) -> None:
        # 新しい版で作り直した設定を、旧い中身で上書きしない（旧版と行き来する人がいる）
        _put(roaming / "Kumiki" / "shortcuts.json", "旧")
        _put(roaming / "Sashimono" / "shortcuts.json", "新")
        migrate_legacy_folders()
        assert (roaming / "Sashimono" / "shortcuts.json").read_text("utf-8") == "新"

    def test_a_copy_that_fails_halfway_leaves_nothing_behind(
        self, roaming: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 半分だけの置き場が新しい名前で残ると、次の起動は「もう在る」と見て
        # 残りを引き継がない 失敗したら何も残さず、次の起動でやり直す
        _put(roaming / "Kumiki" / "a.json")
        _put(roaming / "Kumiki" / "b.json")
        real_copytree = shutil.copytree
        copied: list[str] = []

        def copy_one(source: str, target: str) -> object:
            if copied:
                raise OSError("ディスクがいっぱい")
            copied.append(source)
            return shutil.copy2(source, target)

        def failing_copytree(source: Path, target: Path, **options: Any) -> object:
            return real_copytree(source, target, copy_function=copy_one, **options)

        monkeypatch.setattr(shutil, "copytree", failing_copytree)
        notes = migrate_legacy_folders()
        assert copied, "1 つ目は写せている（途中で止まった形になっていない）"
        assert [n.action for n in notes if n.target == roaming / "Sashimono"] == ["failed"]
        assert not (roaming / "Sashimono").exists()
        assert [p.name for p in roaming.iterdir()] == ["Kumiki"]

        monkeypatch.setattr(shutil, "copytree", real_copytree)
        migrate_legacy_folders()
        assert sorted(p.name for p in (roaming / "Sashimono").iterdir()) == ["a.json", "b.json"]


class TestLocalFolder:
    def test_the_local_folder_is_moved_whole(self, local: Path) -> None:
        # 写すとキャッシュと実行環境（数 GB）で起動が止まる 移すなら一瞬
        _put(local / "Kumiki" / "cache" / "waveform" / "a.peaks.npz")
        _put(local / "Kumiki" / "runtime" / "faster_whisper" / "__init__.py")
        _put(local / "Kumiki" / "backups" / "本編-0123456789" / "20260101-000000-000000-000.kmk")

        (note,) = [n for n in migrate_legacy_folders() if n.target == local / "Sashimono"]
        assert note.action == "moved"
        assert not (local / "Kumiki").exists()
        assert (local / "Sashimono" / "cache" / "waveform" / "a.peaks.npz").is_file()
        assert userdirs.data_root() / "runtime" == local / "Sashimono" / "runtime"
        assert (local / "Sashimono" / "runtime" / "faster_whisper" / "__init__.py").is_file()

    def test_when_it_cannot_move_only_what_cannot_be_remade_is_copied(
        self, local: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 旧版が動いていてファイルを掴んでいると移せない そのときは退避と控えだけを
        # 写す キャッシュは作り直せ、実行環境はボタンで入れ直せる 写すと起動が止まる
        old = local / "Kumiki"
        _put(old / "cache" / "a.peaks.npz")
        _put(old / "runtime" / "big.dll")
        _put(old / "backups" / "本編-0123456789" / "20260101-000000-000000-000.kmk", "控え")
        _put(old / "recovery" / "abc.kmk", "{}")
        _put(old / "recovery" / "live.lock")
        real_rename = Path.rename

        def locked(self: Path, target: Path) -> Path:
            if self == old:
                raise PermissionError("使用中")
            return real_rename(self, target)

        monkeypatch.setattr(Path, "rename", locked)
        (note,) = [n for n in migrate_legacy_folders() if n.target == local / "Sashimono"]
        assert note.action == "copied-partly"

        new = local / "Sashimono"
        assert (new / "backups" / "本編-0123456789" / "20260101-000000-000000-000.kmk").is_file()
        assert (new / "recovery" / "abc.kmk").is_file()
        assert not (new / "cache").exists()
        assert not (new / "runtime").exists()
        # 錠を写すと持ち主の居ない錠になり、旧版の生きた作業を「落ちた」と勧める
        assert not (new / "recovery" / "live.lock").exists()
        assert old.is_dir()

    def test_a_crash_from_the_old_version_is_offered_after_moving(self, local: Path) -> None:
        # 引き継いだあとに復元を勧められなければ、引き継いだ意味が無い
        data = project_to_dict(Project.create(name="落ちた作業"))
        data["format"] = "kumiki-project"
        _put(local / "Kumiki" / "recovery" / "abc.kmk", json.dumps(data, ensure_ascii=False))
        migrate_legacy_folders()
        (entry,) = find_orphans()
        assert load_project(entry.path).name == "落ちた作業"


class TestWhenItRuns:
    def test_the_editor_migrates_before_looking_for_the_runtime(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 逆にすると、旧い置き場に入れた字幕起こしを探し損ね、その起動では
        # 「未導入」と出る（次の起動では見つかる、という分かりにくい形になる）
        import sashimono.app as app

        calls: list[str] = []

        class StopError(Exception):
            pass

        def migrate() -> list[userdirs.MigrationNote]:
            calls.append("migrate")
            return []

        def activate() -> None:
            calls.append("activate")
            raise StopError

        monkeypatch.setattr(app, "migrate_legacy_folders", migrate)
        monkeypatch.setattr(app, "activate_runtime", activate)
        with pytest.raises(StopError):
            app.main(["sashimono"])
        assert calls == ["migrate", "activate"]

    def test_the_self_check_leaves_the_old_folders_alone(
        self, roaming: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 自己診断は本人の置き場を使わない そこで移すと、画面を 1 度も出さないうちに
        # 旧版の置き場が付け替わる
        from sashimono.app import SELF_CHECK_FLAG, main

        _put(roaming / "Kumiki" / "shortcuts.json")
        monkeypatch.setattr("sashimono.selfcheck.main", lambda: 0)
        assert main(["sashimono", SELF_CHECK_FLAG]) == 0
        assert not (roaming / "Sashimono").exists()


class TestNothingToDo:
    def test_a_fresh_install_creates_nothing(self, roaming: Path, local: Path) -> None:
        # 旧い置き場が無ければ何もしない 空の置き場を作ると「引き継ぎ済み」に見える
        assert migrate_legacy_folders() == []
        assert not (roaming / "Sashimono").exists()
        assert not (local / "Sashimono").exists()

    def test_running_twice_does_nothing_the_second_time(self, roaming: Path) -> None:
        # 起動のたびに呼ぶので、2 回目以降は何もしないこと
        _put(roaming / "Kumiki" / "shortcuts.json")
        assert migrate_legacy_folders() != []
        assert migrate_legacy_folders() == []
