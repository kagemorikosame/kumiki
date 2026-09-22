"""文章に句点を使わない、という約束を確かめる道具（tools/punctuation.py）

この道具は書き換えまで行う **データとしての句点まで消すと、字幕の改行位置や
句読点の処理が静かに壊れる** 例外にならず、出来上がった字幕が少し変になるだけ
なので気付きにくい ここでその境目を固定する
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from sashimono.asr import cleanup

ROOT = Path(__file__).resolve().parent.parent
MARU = chr(0x3002)


@pytest.fixture(scope="module")
def tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("punctuation", ROOT / "tools" / "punctuation.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclass は定義の途中で自分のモジュールを sys.modules から引く
    # 登録しないまま実行すると、その場で AttributeError になる
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


class TestData:
    def test_the_subtitle_cleanup_still_knows_the_full_stop(self) -> None:
        # 一括で書き換えたときに、ここの句点まで消えていないこと
        # 消えると字幕が句点の後ろで改行されなくなる
        assert MARU in cleanup._BREAK_AFTER
        assert MARU in cleanup._FORBIDDEN_AT_LINE_START

    @pytest.mark.parametrize(
        "definition",
        ["_LEADING_PUNCTUATION =", "_BREAK_AFTER =", "_FORBIDDEN_AT_LINE_START =", "re.sub("],
    )
    def test_the_cleanup_literals_are_not_checked(self, tool: ModuleType, definition: str) -> None:
        # どれか 1 つでも書き換えられると、字幕の改行位置・行頭禁則・読点の扱いが
        # 変わる 例外にはならず、出来上がった字幕が少し変になるだけで気付きにくい
        path = ROOT / "src" / "sashimono" / "asr" / "cleanup.py"
        lines = path.read_text(encoding="utf-8").splitlines()
        targets = {number for number, line in enumerate(lines, 1) if definition in line}
        assert targets, f"{definition} が見つからない（名前が変わったならここも直す）"
        assert not {hit.line for hit in tool.scan(path)} & targets

    def test_string_literals_in_tests_are_data(
        self, tool: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # テストの文字列は入力と期待値 字幕文の句点をここで消すと、
        # 検査している中身そのものが変わる
        tests = tmp_path / "tests"
        tests.mkdir()
        path = write(tests, "test_x.py", f'TEXT = "今日は{MARU}"\n')
        monkeypatch.setattr(tool, "ROOT", tmp_path)
        assert tool.scan(path) == []

    def test_string_literals_in_tools_are_prose(
        self, tool: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 道具の出力も人が読む 壊れると verify.py などの端末に出る文言が検査から漏れる
        # 実際に外していた間、「すべて通過」に句点が残っていた
        tools = tmp_path / "tools"
        tools.mkdir()
        path = write(tools, "x.py", f'print("通過{MARU}")\n')
        monkeypatch.setattr(tool, "ROOT", tmp_path)
        assert len(tool.scan(path)) == 1


class TestProse:
    @pytest.fixture(autouse=True)
    def _inside(self, tool: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(tool, "ROOT", tmp_path)

    def scan(self, tool: ModuleType, tmp_path: Path, text: str) -> str:
        path = write(tmp_path, "x.md", text)
        tool.fix(path, tool.scan(path))
        return path.read_text(encoding="utf-8")

    def test_the_end_of_a_line_just_loses_it(self, tool: ModuleType, tmp_path: Path) -> None:
        # 空白を残すと行末に空白が溜まり、差分と整形の検査が汚れる
        assert self.scan(tool, tmp_path, f"終わる{MARU}\n") == "終わる\n"

    def test_two_sentences_on_a_line_get_a_space(self, tool: ModuleType, tmp_path: Path) -> None:
        # 消すだけにすると、次の文と地続きになって読めない
        assert self.scan(tool, tmp_path, f"一つ目{MARU}二つ目\n") == "一つ目 二つ目\n"

    def test_before_a_closing_bracket_it_just_goes(self, tool: ModuleType, tmp_path: Path) -> None:
        # 空白を入れると「言う 」と括弧の内側に空きができて不自然になる
        assert self.scan(tool, tmp_path, f"「言う{MARU}」\n") == "「言う」\n"

    def test_closing_bold_sticks_to_the_sentence(self, tool: ModuleType, tmp_path: Path) -> None:
        # 閉じの前に空白を入れると Markdown の太字が閉じず、後ろの文まで太字になる
        assert self.scan(tool, tmp_path, f"**大事{MARU}** 次\n") == "**大事** 次\n"

    def test_opening_bold_keeps_a_space(self, tool: ModuleType, tmp_path: Path) -> None:
        # 閉じと同じ扱いにすると「書く**次の文**」と詰まる 一度そうなった
        assert self.scan(tool, tmp_path, f"書く{MARU}**次の文**\n") == "書く **次の文**\n"

    def test_opening_code_keeps_a_space(self, tool: ModuleType, tmp_path: Path) -> None:
        # 太字と同じ取り違えがコードの記号でも起きた「受ける`main` へ」
        assert self.scan(tool, tmp_path, f"受ける{MARU}`main` へ\n") == "受ける `main` へ\n"

    def test_bold_that_spans_lines_still_closes(self, tool: ModuleType, tmp_path: Path) -> None:
        # 行の頭から数えると、改行をまたぐ太字の閉じを開きと取り違えて空白を入れる
        # 閉じの前に空白があると太字が閉じない
        text = f"前 **落ちたら\n終了コードが非 0{MARU}** 次\n"
        assert self.scan(tool, tmp_path, text) == "前 **落ちたら\n終了コードが非 0** 次\n"


class TestSafety:
    def test_line_endings_are_kept(
        self, tool: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 改行を揃え直すと、句点と関係の無い行まで全部が差分になる
        # Windows の read_text / write_text の往復でこれが起きる
        monkeypatch.setattr(tool, "ROOT", tmp_path)
        path = tmp_path / "crlf.md"
        crlf = chr(13) + chr(10)
        path.write_bytes(f"一つ目{MARU}{crlf}二つ目{crlf}".encode())
        tool.fix(path, tool.scan(path))
        assert path.read_bytes() == f"一つ目{crlf}二つ目{crlf}".encode()

    def test_the_second_half_of_a_joined_docstring_is_prose(
        self, tool: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 開始位置だけで docstring を見分けると、つないで書いた後半を取りこぼす
        # 取りこぼした句点は検査にも書き換えにも掛からず、そのまま残る
        monkeypatch.setattr(tool, "ROOT", tmp_path)
        (tmp_path / "src").mkdir()
        path = tmp_path / "src" / "x.py"
        source = ["def f() -> None:", f'    "前半" "後半{MARU}"', ""]
        path.write_text(chr(10).join(source), encoding="utf-8")
        assert len(tool.scan(path)) == 1

    def test_a_symlink_is_not_followed(
        self, tool: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 辿ると、PR に混ぜたリンク 1 つで --fix がリポジトリの外を書き換える
        outside = tmp_path / "outside.md"
        outside.write_text(f"外{MARU}", encoding="utf-8")
        repo = tmp_path / "repo"
        repo.mkdir()
        try:
            (repo / "link.md").symlink_to(outside)
        except OSError:
            pytest.skip("この環境ではシンボリックリンクを作れない（Windows の開発者モードが無い）")
        monkeypatch.setattr(tool, "ROOT", repo)
        assert tool.targets(repo) == []
        with pytest.raises(ValueError, match="外"):
            tool.fix(repo / "link.md", [])
