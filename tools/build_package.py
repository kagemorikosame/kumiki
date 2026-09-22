r"""配る zip を作る

    .venv\Scripts\python.exe tools\build_package.py
    .venv\Scripts\python.exe tools\build_package.py --skip-build   （組み立て済みを zip にし直す）

やること

1. PyInstaller で ``Kumiki.exe`` と部品一式（``_internal``）を組み立てる
2. 組み立ての記録から、積んだファイルがどの包みから来たかを辿り、包みごとの
   使用許諾の写しを ``licenses`` へ集める 出どころの分からないファイルがあれば止まる
3. 隣にスクリプト置き場（``scripts``）と説明書き・使用許諾の一覧を置く
4. ``dist\Kumiki-<版>-windows-x64.zip`` にまとめる
5. **できた zip を別の場所へ展開し、中の exe で ``--self-check`` を走らせる**
   組み立てた直後のフォルダで確かめると、開発環境の DLL や Python を
   拾って通ってしまう 配るのは zip なので、zip から確かめる

確かめるときの環境変数は最小にする（``PATH`` は Windows の分だけ）
開発機の ``PATH`` に FFmpeg や Python が載っていると、積み忘れがあっても通る

**依存が何も入っていない機械での確認**は、ここではできない（VC++ ランタイムなど
Windows 側の部品は開発機に入っている） 本人の確認に回す
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import importlib.metadata
import io
import locale
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Iterable, Iterator, Mapping, Sequence
from pathlib import Path, PurePosixPath

if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from kumiki import __version__  # noqa: E402
from kumiki.app import SELF_CHECK_FLAG  # noqa: E402
from kumiki.compat.aviutl.catalog import PORTABLE_SCRIPTS_DIR  # noqa: E402

#: exe と、zip を展開したときのフォルダの名前
APP_NAME = "Kumiki"

#: 同梱しないもの 追加機能（字幕起こし・AI 連携）は画面のボタンから後で入れる
#: 開発機に入っていると PyInstaller が拾ってしまい、zip が 2 GB を超える
EXCLUDED_MODULES = (
    "faster_whisper",
    "ctranslate2",
    "torch",
    "nvidia",
    "claude_agent_sdk",
    # 開発の道具 動かすのに要らない
    "pytest",
    "hypothesis",
    "mypy",
    "PyInstaller",
)

#: 読み込み方が動的で、PyInstaller が辿れないもの **まとめて積む**
#: lupa は AviUtl に近い Lua を名前で選んで読む（``import_module("lupa.lua51")``）
#: pip は導入ボタンが使う 中身を名前で引くうえ、HTTPS の証明書（データ）も要る
COLLECTED_PACKAGES = ("lupa", "pip")

#: exe の隣に置く説明書き
README_TEXT = f"""Kumiki {__version__}

起動: Kumiki.exe

AviUtl のスクリプト（.anm2 .obj2 など）は、この隣の {PORTABLE_SCRIPTS_DIR} フォルダへ
置けば読み込まれます AviUtl2 が入っていれば、そちらの Script フォルダも読みます

動かないときは、このフォルダでコマンドを開いて次を打つと、どの部品が
動いていないかが 1 行ずつ出ます（そのまま打つと、結果は窓で出ます）

    Kumiki.exe {SELF_CHECK_FLAG} | more

字幕起こしと AI 連携は、ソフトの中のボタンから必要になったときに入れます
（最初から入れると 2 GB を超えるため）

使用許諾: Kumiki 本体は MIT（LICENSE.txt） 一緒に入れている部品に GPL の物が
あるため、この zip は全体として GPL の条件で配っています 部品ごとの使用許諾と
ソースの入手先は THIRD_PARTY_NOTICES.txt と licenses フォルダにあります
"""

SCRIPTS_README = f"""AviUtl のスクリプトの置き場です

ここへ .anm2 .obj2 .cam2 .scn2 .tra2（と AviUtl1 世代の .anm .obj）を置くと、
Kumiki を起動し直したときに読み込まれます フォルダに分けて置いても読みます

AviUtl2 が入っている機械では、AviUtl2 の Script フォルダも同じように読みます
（こちらへ写す必要はありません）

動いているか確かめるには Kumiki.exe {SELF_CHECK_FLAG}
"""

#: 使用許諾の写しを置くフォルダ exe の隣とリポジトリの直下で同じ名前にする
#: リポジトリの方には、どの包みも写しを持っていない物（GNU の全文、PyAV の wheel に
#: 入っている DLL と LuaJIT の写し）を置いてある 出どころは THIRD_PARTY_NOTICES.md
LICENSES_DIR = "licenses"

#: 同梱した部品の一覧 リポジトリの THIRD_PARTY_NOTICES.md を、Windows で開きやすい名前で置く
NOTICES_SOURCE = ROOT / "THIRD_PARTY_NOTICES.md"
NOTICES_NAME = "THIRD_PARTY_NOTICES.txt"

#: 使用許諾の写しを dist-info に持っていない包み（配布名を小文字で）
#: 写しが無いことは一覧（THIRD_PARTY_NOTICES.md）に書いてある 足すときも、先に一覧へ
#: 書いてからここへ足す 黙って足すと、写しの無い物を無いまま配ることになる
WITHOUT_LICENSE_FILES = frozenset({"pyopengl"})

#: いつも積む包み ``Kumiki.exe`` の起動部そのものが PyInstaller の物で、組み立ての記録では
#: 作業フォルダの exe として出てくるため、ファイルを辿っても包みに行き着かない
ALWAYS_BUNDLED = ("pyinstaller",)


def pyinstaller_arguments(work: Path, dist: Path) -> list[str]:
    """PyInstaller へ渡す引数

    画面のアプリなので窓を出さない（``--windowed``） 1 ファイルにまとめる形
    （``--onefile``）は使わない 起動のたびに一時フォルダへ全部を展開するので、
    300 MB 近い部品だと起動に数秒余計に掛かり、ウイルス対策にも引っかかりやすい
    もう 1 つ、Qt を LGPL-3.0 で使う以上、使う人が Qt の DLL を差し替えられなければ
    ならない onedir なら DLL は別のファイルのまま置かれる
    """
    arguments = [
        "--noconfirm",
        "--clean",
        "--windowed",
        "--onedir",
        "--name",
        APP_NAME,
        "--icon",
        str(ROOT / "src" / "kumiki" / "resources" / "kumiki.ico"),
        "--paths",
        str(ROOT / "src"),
        "--workpath",
        str(work),
        "--distpath",
        str(dist),
        "--specpath",
        str(work),
        # アイコンやロゴ（.ico .svg） importlib.resources で引くので、
        # データとして積まないと窓のアイコンが出ない
        "--collect-data",
        "kumiki.resources",
    ]
    for package in COLLECTED_PACKAGES:
        arguments += ["--collect-all", package]
    for module in EXCLUDED_MODULES:
        arguments += ["--exclude-module", module]
    arguments.append(str(ROOT / "src" / "kumiki" / "__main__.py"))
    return arguments


def assemble(bundle: Path) -> None:
    """組み立てたフォルダへ、使う人が触る物を足す"""
    scripts = bundle / PORTABLE_SCRIPTS_DIR
    scripts.mkdir(exist_ok=True)
    (scripts / "README.txt").write_text(SCRIPTS_README, encoding="utf-8")
    (bundle / "README.txt").write_text(README_TEXT, encoding="utf-8")
    shutil.copyfile(ROOT / "LICENSE", bundle / "LICENSE.txt")
    shutil.copyfile(NOTICES_SOURCE, bundle / NOTICES_NAME)
    for relative in repository_license_files():
        destination = bundle / LICENSES_DIR / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / LICENSES_DIR / relative, destination)


def repository_license_files() -> list[str]:
    """リポジトリの ``licenses`` に置いた写し（``licenses`` からの相対の綴り）

    GNU の全文と、wheel が写しを持っていない部品（av.libs の DLL・LuaJIT）の写し
    zip から確かめるときの見本にもする
    """
    root = ROOT / LICENSES_DIR
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())


def bundled_sources(record: Path) -> list[Path]:
    """組み立ての記録（PyInstaller の TOC）から、積んだファイルの元の場所を読む

    exe の隣に並ぶファイル（``COLLECT``）だけでは足りない 純 Python の包みは
    exe の中の書庫（``PYZ``）に入り、フォルダには現れない
    """
    collected = ast.literal_eval((record / "COLLECT-00.toc").read_text(encoding="utf-8"))[0]
    _, modules = ast.literal_eval((record / "PYZ-00.toc").read_text(encoding="utf-8"))
    sources: list[Path] = []
    for entry in [*collected, *modules]:
        source = entry[1]
        # 名前空間の包みはファイルを持たず "-" で記録される 辿る物が無い
        if source and source != "-":
            sources.append(Path(source))
    return sources


def _key(path: Path) -> str:
    # 記録の綴りと dist-info の綴りで大文字小文字や区切りが揺れる 揃えないと同じ
    # ファイルを別物と見て、出どころが分からないと言って止まる
    # resolve は使わない 数千のファイルごとに実体を辿ると遅く、辿らなくても揃う
    return os.path.normcase(os.path.normpath(path.absolute()))


def _inside(path: Path, root: Path) -> bool:
    return _key(path).startswith(_key(root) + os.sep)


def distribution_owners() -> dict[str, importlib.metadata.Distribution]:
    """入っているファイル 1 つずつから、それを入れた包みを引く表"""
    owners: dict[str, importlib.metadata.Distribution] = {}
    for distribution in importlib.metadata.distributions():
        for file in distribution.files or ():
            owners[_key(Path(str(file.locate())))] = distribution
    return owners


def license_files(distribution: importlib.metadata.Distribution) -> list[tuple[str, Path]]:
    """包みが dist-info に持っている使用許諾の写し（置く名前、元の場所）

    名前は METADATA の ``License-File`` から引く PEP 639 以降は ``licenses`` の下、
    それより前は dist-info の直下に置かれる 書いていない古い包みは名前で拾う
    """
    inside: dict[PurePosixPath, Path] = {}
    for file in distribution.files or ():
        if len(file.parts) > 1 and file.parts[0].endswith(".dist-info"):
            inside[PurePosixPath(*file.parts[1:])] = Path(str(file.locate()))
    declared = distribution.metadata.get_all("License-File") or []
    found: list[tuple[str, Path]] = []
    for name in declared:
        for candidate in (PurePosixPath("licenses", name), PurePosixPath(name)):
            if candidate in inside:
                found.append((name, inside[candidate]))
                break
    if not declared:
        found = [
            (str(relative), path)
            for relative, path in inside.items()
            if relative.name.upper().startswith(("LICEN", "COPYING", "NOTICE"))
        ]
    return found


def collect_licenses(
    bundle: Path,
    sources: Iterable[Path],
    own_roots: Sequence[Path],
    *,
    owners: Mapping[str, importlib.metadata.Distribution] | None = None,
    notices: str | None = None,
) -> list[str]:
    """積んだファイルの出どころの包みを数え、使用許諾の写しを ``licenses`` へ集める

    戻り値は配れない理由の一覧 空なら配ってよい 包みの名前を決め打ちの一覧で持つと、
    組み立てる機械に入っている包みが変わったとき（PyInstaller は入っていれば拾う）に
    写しの無い物を黙って配る そのため毎回、実際に積んだ物から数える

    ``own_roots`` は Kumiki 自身のファイルの置き場（ソースと組み立ての作業フォルダ）
    Python 本体のファイルは ``sys.base_prefix`` の下にあり、使用許諾はそこの
    ``LICENSE.txt`` を写す
    """
    owners = distribution_owners() if owners is None else owners
    notices = NOTICES_SOURCE.read_text(encoding="utf-8") if notices is None else notices
    python_root = Path(sys.base_prefix)
    found: dict[str, importlib.metadata.Distribution] = {}
    problems: list[str] = []
    for source in sources:
        distribution = owners.get(_key(source))
        if distribution is not None:
            found.setdefault(distribution.metadata["Name"].lower(), distribution)
        elif not any(_inside(source, root) for root in (*own_roots, python_root)):
            # 開発機の PATH から拾った DLL などがここに来る どの使用許諾で配るのか
            # 決められない物は配らない
            problems.append(f"出どころの分からないファイルを積んでいる: {source}")
    for name in ALWAYS_BUNDLED:
        with contextlib.suppress(importlib.metadata.PackageNotFoundError):
            found.setdefault(name, importlib.metadata.distribution(name))

    target = bundle / LICENSES_DIR
    for key, distribution in sorted(found.items()):
        name = distribution.metadata["Name"]
        # 一覧に書いていない包みは、ソースの入手先も書いていない 一覧を直してから配る
        if f"`{name.lower()}`" not in notices.lower():
            problems.append(f"{name} が {NOTICES_SOURCE.name} の一覧に無い")
        files = license_files(distribution)
        if not files and key not in WITHOUT_LICENSE_FILES:
            problems.append(f"{name} {distribution.version} の使用許諾の写しが見つからない")
        folder = target / f"{name}-{distribution.version}"
        for relative, path in files:
            destination = folder / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)

    python_license = python_root / "LICENSE.txt"
    if python_license.exists():
        (target / "Python").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(python_license, target / "Python" / "LICENSE.txt")
    else:
        problems.append(f"Python 本体の使用許諾が無い: {python_license}")
    return problems


def missing_notices(home: Path) -> list[str]:
    """展開した zip に、使用許諾の一覧と全文がそろっているか

    GPL の部品を積んで配る以上、使用許諾の全文を一緒に渡さなければならない
    組み立ての途中の段を飛ばしても zip は作れてしまうので、配る物の側で確かめる
    """
    expected = [
        "LICENSE.txt",
        NOTICES_NAME,
        f"{LICENSES_DIR}/Python/LICENSE.txt",
        *(f"{LICENSES_DIR}/{name}" for name in repository_license_files()),
    ]
    return [name for name in expected if not (home / name).is_file()]


def make_zip(bundle: Path, target: Path) -> Path:
    """フォルダごと zip にする 展開すると ``Kumiki\\`` が 1 つできる形

    中身をばらで入れると、展開した場所に部品が散らばる
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    # 前の組み立ての zip を先に消す 残したまま今回が途中で落ちると、
    # 前の物が今回の版の名前で残り、新しい物と取り違えて配ることになる
    # （消しても組み立て直せば戻る） 開いたままで消せなければ、ここで止まる
    target.unlink(missing_ok=True)
    temporary = target.with_name(target.name + ".writing")
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(bundle.rglob("*")):
                if path.is_file():
                    archive.write(path, Path(APP_NAME) / path.relative_to(bundle))
        # 途中で止まった zip を完成品と取り違えないよう、書き終えてから名前を付ける
        # 名前を付ける所も後始末の中に入れる 前の zip を開いたままだと Windows は
        # 置き換えを断り、書き終えた 100 MB がそのまま残る
        temporary.replace(target)
    except BaseException:
        # 書きかけも残さない 名前が違うので完成品とは取り違えないが、
        # 100 MB ずつ溜まるうえ、手で配るときに紛れる
        temporary.unlink(missing_ok=True)
        raise
    return target


def minimal_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """確かめるときの環境変数 Windows が動くのに要る分だけ残す

    開発機の ``PATH`` には Python や FFmpeg が載っている 残したまま確かめると、
    zip に積み忘れた DLL をそちらから拾って通ってしまう
    """
    keep = (
        "SYSTEMROOT",
        "WINDIR",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "PROGRAMDATA",
        "HOMEDRIVE",
        "HOMEPATH",
        "USERNAME",
        "COMPUTERNAME",
    )
    upper = {key.upper(): value for key, value in environ.items()}
    minimal = {key: upper[key] for key in keep if key in upper}
    system_root = minimal.get("SYSTEMROOT", r"C:\Windows")
    minimal["PATH"] = os.pathsep.join(
        [
            str(Path(system_root) / "System32"),
            system_root,
            str(Path(system_root) / "System32" / "Wbem"),
        ]
    )
    return minimal


#: zip から確かめるときに置き場へ置く見本 読まれた本数で確かめる
SAMPLE_SCRIPT_NAME = "確かめる用.anm2"
SAMPLE_SCRIPT = """--track@amount:量,0,100,50
obj.ox = amount
"""

#: pip で入れてみる小さな包み ネットにつながずに入れられるよう、ここで作る
SAMPLE_PACKAGE = "kumiki_check_sample"


def write_sample_wheel(folder: Path) -> Path:
    """中身が 1 行だけの wheel を作る

    導入ボタンと同じ ``--target`` の入れ方を、ネットにつながずに通すため
    PyPI から落とすと、確かめるたびに外へ取りに行くことになる
    """
    name = f"{SAMPLE_PACKAGE}-0.1-py3-none-any.whl"
    info = f"{SAMPLE_PACKAGE}-0.1.dist-info"
    files = {
        f"{SAMPLE_PACKAGE}/__init__.py": "VALUE = 1\n",
        f"{info}/METADATA": f"Metadata-Version: 2.1\nName: {SAMPLE_PACKAGE}\nVersion: 0.1\n",
        f"{info}/WHEEL": "Wheel-Version: 1.0\nGenerator: kumiki\nRoot-Is-Purelib: true\n"
        "Tag: py3-none-any\n",
    }
    record = "".join(f"{path},,\n" for path in files) + f"{info}/RECORD,,\n"
    target = folder / name
    with zipfile.ZipFile(target, "w") as wheel:
        for path, text in files.items():
            wheel.writestr(path, text)
        wheel.writestr(f"{info}/RECORD", record)
    return target


def _run(executable: Path, arguments: list[str], folder: str) -> subprocess.CompletedProcess[str]:
    """展開した exe を走らせる

    渡すのは、この道具が展開した exe の場所と、この道具の中で決めた引数だけ
    外から来た文字列は混ざらず、shell も通さない（引数は並びのまま渡す）
    """
    # 監査済み 引数の出どころは上のとおりで、外から来た文字列は混ざらない
    return subprocess.run(  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
        [str(executable), *arguments],
        cwd=folder,
        env=minimal_environment(os.environ),
        capture_output=True,
        text=True,
        # exe はこの機械の既定の文字コード（日本語の Windows なら cp932）で書く
        # 配布版は ``PYTHONIOENCODING`` を見ない（PyInstaller が環境変数から
        # 切り離している 実際に渡して確かめた）ので、読む側を合わせる
        # 使う人がコマンドで結果を見るときも、この既定の文字コードで正しく出る
        # getpreferredencoding は使わない この道具を UTF-8 モード（PYTHONUTF8=1）で
        # 動かすと UTF-8 を返し、exe の書いた cp932 を読み違える
        encoding=locale.getencoding(),
        errors="replace",
        timeout=600,
        check=False,
    )


def smoke_test(archive: Path) -> int:
    """zip を別の場所へ展開し、中の exe で確かめる

    1. 自己診断（描く・書き出す・Lua・pip の有無）
    2. exe の隣の置き場へ見本を置き、**読まれた**こと
    3. exe に pip を走らせ、導入ボタンと同じ入れ方で**実際に入る**こと
    """
    with tempfile.TemporaryDirectory(prefix="kumiki-zip-check-") as folder:
        with zipfile.ZipFile(archive) as opened:
            opened.extractall(folder)
        home = Path(folder) / APP_NAME
        executable = home / f"{APP_NAME}.exe"
        print(f"展開した先で確かめる: {executable}")

        (home / PORTABLE_SCRIPTS_DIR / SAMPLE_SCRIPT_NAME).write_text(
            SAMPLE_SCRIPT, encoding="utf-8"
        )
        checked = _run(executable, [SELF_CHECK_FLAG], folder)
        print(checked.stdout.rstrip())
        if checked.stderr.strip():
            print(checked.stderr.rstrip())
        failures = [] if checked.returncode == 0 else ["自己診断"]
        failures += [f"使用許諾が入っていない: {name}" for name in missing_notices(home)]

        beside = f"{home / PORTABLE_SCRIPTS_DIR}（1 本）"
        if beside not in checked.stdout:
            failures.append("exe の隣の置き場に置いたスクリプトが読まれていない")

        wheel = write_sample_wheel(Path(folder))
        target = Path(folder) / "runtime"
        installed = _run(
            executable,
            ["-m", "pip", "install", "--no-index", "--target", str(target), str(wheel)],
            folder,
        )
        # 終了コードも見る ファイルを置いたあとで落ちた pip を「入った」と数えない
        if installed.returncode == 0 and (target / SAMPLE_PACKAGE / "__init__.py").exists():
            print(f"[ok] exe の pip で入れられた: {target}")
        else:
            print(installed.stdout.rstrip())
            print(installed.stderr.rstrip())
            failures.append("exe の pip で入れられない（導入ボタンが動かない）")

        for failure in failures:
            print(f"[NG] {failure}")
        return 1 if failures else 0


def package(bundle: Path, target: Path, *, check: bool = True) -> int:
    """組み立てたフォルダを zip にし、zip から確かめる 戻り値は終了コード"""
    assemble(bundle)
    archive = make_zip(bundle, target)
    print(f"できた: {archive}（{_size(archive)} 展開すると {_size(bundle)}）")
    if not check:
        return 0
    try:
        result = smoke_test(archive)
    except BaseException:
        # 確かめる途中で落ちた（展開できない・exe が返ってこない）ときも同じ
        # 確かめ終えていない zip を完成品の名前で残さない
        archive.unlink(missing_ok=True)
        raise
    if result != 0:
        # 確かめて落ちた zip は残さない **dist に zip がある＝確かめ済み** に
        # そろえる 残すと、動かない物を完成品と取り違えて配る 中身を調べたい
        # ときは、組み立てたフォルダがそのまま残っている
        archive.unlink(missing_ok=True)
        print(f"確かめて落ちたので {archive.name} を消した（{bundle} は残してある）")
    return result


def _size(path: Path) -> str:
    total = (
        path.stat().st_size
        if path.is_file()
        else sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
    )
    return f"{total / (1024 * 1024):.0f} MB"


def main(argv: list[str] | None = None, *, dist: Path | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-build", action="store_true", help="組み立て済みを使う")
    parser.add_argument(
        "--skip-check",
        action="store_true",
        help="zip からの確認を省く（できた zip は確かめていない物になる）",
    )
    args = parser.parse_args(argv)

    work = ROOT / "build" / "pyinstaller"
    dist = dist if dist is not None else ROOT / "dist"
    bundle = dist / APP_NAME
    target = dist / f"{APP_NAME}-{__version__}-windows-x64.zip"

    # **組み立てる前に**前の zip を消す 組み立て（PyInstaller）で落ちると
    # zip を作る所まで進まないので、そこで消していては前の物が残り、
    # 今回の完成品に見えてしまう
    target.unlink(missing_ok=True)

    if not args.skip_build:
        import PyInstaller.__main__

        with _without_developer_path():
            PyInstaller.__main__.run(pyinstaller_arguments(work, dist))
    if not (bundle / f"{APP_NAME}.exe").exists():
        print(f"{bundle} に {APP_NAME}.exe が無い 組み立てに失敗している")
        return 1

    record = work / APP_NAME
    try:
        sources = bundled_sources(record)
    except FileNotFoundError:
        # 記録が無いと、何を積んだかを数えられず使用許諾をそろえられない
        print(f"{record} に組み立ての記録が無い --skip-build を外して組み立て直す")
        return 1
    problems = collect_licenses(bundle, sources, (ROOT / "src", record))
    if problems:
        for problem in problems:
            print(f"[NG] {problem}")
        return 1

    return package(bundle, target, check=not args.skip_check)


@contextlib.contextmanager
def _without_developer_path() -> Iterator[None]:
    """組み立てる間だけ ``PATH`` を Windows の分にする

    PyInstaller は Qt の通信部品のために OpenSSL の DLL を ``PATH`` から探す
    開発機では Git for Windows の物（``C:\\Program Files\\Git\\mingw64\\bin``）が
    見つかって積まれていた Kumiki は Qt で通信しないので要らないうえ、出どころが
    組み立てる機械で変わる物は使用許諾をそろえられない
    """
    before = os.environ.get("PATH")
    os.environ["PATH"] = minimal_environment(os.environ)["PATH"]
    try:
        yield
    finally:
        if before is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = before


if __name__ == "__main__":
    raise SystemExit(main())
