r"""配る zip に積んだ GPL / LGPL の部品の、対応するソースを集める（リリースのときに使う）

    .venv\Scripts\python.exe tools\collect_sources.py --check   （入手先に届くかだけ見る）
    .venv\Scripts\python.exe tools\collect_sources.py           （dist\sources へ落とす）

zip は GPL の条件で配る（THIRD_PARTY_NOTICES.md） GPL と LGPL は、バイナリを配る側が
対応するソースを渡せる状態を保つことを求める 上流の置き場は消えたり移ったりするので、
リリースのたびに**積んだのと同じ版**のソースを落とし、同じ GitHub Release へ添付する

落とした物は ``dist\sources`` に置き、sha256 の一覧と manifest（取得元・版・sha256）を書く
上流が sha256 を公開している物（pyav-ffmpeg の組み立て設定に書かれている物）は照合し、
合わなければ消して止まる

版は手で書いてある 積んでいる部品の版と食い違ったまま落とすと、別の版のソースを
添付することになるので、落とす前に入っている PyAV と PySide6 の版と突き合わせる
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO

if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent

#: 積んでいる FFmpeg（PyAV 18.1.0 の wheel は pyav-ffmpeg 8.1.2-1 の組み立てを使う）
FFMPEG_VERSION = "8.1.2"
#: 積んでいる Qt と PySide6
QT_VERSION = "6.11.2"
_QT = f"https://download.qt.io/official_releases/qt/6.11/{QT_VERSION}/submodules"


@dataclass(frozen=True, slots=True)
class Source:
    """落とすソース 1 つ"""

    name: str
    version: str
    url: str
    #: 上流が公開している sha256 無い物は落としたときの値を manifest に残す
    sha256: str | None
    license: str
    #: 保存するファイルの名前 URL の末尾がファイル名にならない物（コミットの tarball）に使う
    filename: str


#: 添付するソースの一覧 版と sha256 は pyav-ffmpeg 8.1.2-1 の ``scripts/pkg.py`` から写した
#: （PyAV 18.1.0 の ``scripts/ffmpeg-8.1.json`` がこの組み立てを指している）
SOURCES: tuple[Source, ...] = (
    Source(
        "FFmpeg",
        FFMPEG_VERSION,
        f"https://ffmpeg.org/releases/ffmpeg-{FFMPEG_VERSION}.tar.xz",
        "464beb5e7bf0c311e68b45ae2f04e9cc2af88851abb4082231742a74d97b524c",
        "LGPL-3.0-or-later（--enable-version3）",
        f"ffmpeg-{FFMPEG_VERSION}.tar.xz",
    ),
    Source(
        "pyav-ffmpeg",
        "8.1.2-1",
        "https://github.com/PyAV-Org/pyav-ffmpeg/archive/refs/tags/8.1.2-1.tar.gz",
        # 組み立ての手順と FFmpeg・LAME・libvpx へ当てた差分 ソースと一緒に渡さないと、
        # 積んだ DLL と同じ物を組み立て直せない
        None,
        "BSD-3-Clause（組み立ての手順と差分）",
        "pyav-ffmpeg-8.1.2-1.tar.gz",
    ),
    Source(
        "x264",
        "b35605ace3ddf7c1a5d67a2eb553f034aef41d55",
        # 本家（code.videolan.org）は道具からの取得をボット避けの画面で断る
        # 同じコミットを持つ GitHub の写しから落とす（中身はコミットで一意に決まる
        # tarball の作り方が違うので、本家の sha256 とは合わない）
        "https://github.com/mirror/x264/archive/b35605ace3ddf7c1a5d67a2eb553f034aef41d55.tar.gz",
        None,
        "GPL-2.0-or-later",
        "x264-b35605ace3ddf7c1a5d67a2eb553f034aef41d55.tar.gz",
    ),
    Source(
        "x265",
        "4.2",
        "https://bitbucket.org/multicoreware/x265_git/downloads/x265_4.2.tar.gz",
        "40b1ea0453e0309f0eba934e0ddf533f8f6295966679e8894e8f1c1c8d5e1210",
        "GPL-2.0-or-later",
        "x265_4.2.tar.gz",
    ),
    Source(
        "LAME",
        "3.100",
        "http://deb.debian.org/debian/pool/main/l/lame/lame_3.100.orig.tar.gz",
        "ddfe36cab873794038ae2c1210557ad34857a4b6bdc515785d1da9e175b1da1e",
        "LGPL-2.0-or-later",
        "lame_3.100.orig.tar.gz",
    ),
    Source(
        "libiconv",
        "1.19",
        "https://ftp.gnu.org/pub/gnu/libiconv/libiconv-1.19.tar.gz",
        None,
        "LGPL-2.1-or-later",
        "libiconv-1.19.tar.gz",
    ),
    *(
        Source(
            f"Qt {module}",
            QT_VERSION,
            f"{_QT}/{module}-everywhere-src-{QT_VERSION}.tar.xz",
            None,
            "LGPL-3.0-only",
            f"{module}-everywhere-src-{QT_VERSION}.tar.xz",
        )
        # 積んでいる Qt の DLL とプラグインが属するモジュール Qt6Pdf（qpdf）は
        # qtwebengine に入っている
        for module in (
            "qtbase",
            "qtdeclarative",
            "qtsvg",
            "qtimageformats",
            "qtvirtualkeyboard",
            "qtwebengine",
        )
    ),
    Source(
        "PySide6 / shiboken6",
        QT_VERSION,
        "https://download.qt.io/official_releases/QtForPython/pyside6/"
        f"PySide6-{QT_VERSION}-src/pyside-setup-everywhere-src-{QT_VERSION}.tar.xz",
        None,
        "LGPL-3.0-only",
        f"pyside-setup-everywhere-src-{QT_VERSION}.tar.xz",
    ),
)

#: sha256 の一覧と manifest の名前 Release には zip と並べて添付するので、ソースの物と分かる
#: 名前にする `SHA256SUMS` だけだと zip の物と取り違える
SUMS_NAME = "sources-SHA256SUMS.txt"
MANIFEST_NAME = "sources-manifest.json"

#: 落とすときに名乗る名前 名乗らないと断る置き場がある
USER_AGENT = "kumiki-collect-sources"

Opener = Callable[[urllib.request.Request], IO[bytes]]


def _open(request: urllib.request.Request) -> IO[bytes]:
    # 開く先は上の一覧に書いた URL だけ 外から来た文字列は混ざらない
    response: IO[bytes] = urllib.request.urlopen(request, timeout=120)
    return response


def installed_mismatches() -> list[str]:
    """一覧の版と、いま入っている（＝zip に積む）部品の版の食い違い

    PyAV や PySide6 を上げたのに一覧を直し忘れると、別の版のソースを添付してしまう
    """
    import av._core
    import PySide6

    problems = []
    ffmpeg = str(av._core.ffmpeg_version_info)
    if ffmpeg != FFMPEG_VERSION:
        problems.append(f"積んでいる FFmpeg は {ffmpeg} 一覧は {FFMPEG_VERSION}")
    if PySide6.__version__ != QT_VERSION:
        problems.append(f"積んでいる PySide6 は {PySide6.__version__} 一覧は {QT_VERSION}")
    return problems


def _request(url: str, *, method: str = "GET", ranged: bool = False) -> urllib.request.Request:
    headers = {"User-Agent": USER_AGENT}
    if ranged:
        headers["Range"] = "bytes=0-0"
    return urllib.request.Request(url, headers=headers, method=method)


def _is_archive(response: IO[bytes]) -> bool:
    # ボット避けの画面は 200 で HTML を返す 届いたと数えると、HTML を添付して配る
    headers = getattr(response, "headers", None)
    kind = headers.get("Content-Type", "") if headers is not None else ""
    return "text/html" not in kind


def check_urls(sources: Iterable[Source], *, opener: Opener = _open) -> list[str]:
    """入手先に届くかを見る 中身は落とさない

    HEAD を断る置き場（Bitbucket の署名付きの置き場は HEAD に 403 を返す）には、
    先頭 1 バイトだけを頼む
    """
    problems = []
    for source in sources:
        reached = False
        for request in (
            _request(source.url, method="HEAD"),
            _request(source.url, ranged=True),
        ):
            try:
                with opener(request) as response:
                    reached = _is_archive(response)
            except (urllib.error.URLError, OSError):
                continue
            if reached:
                break
        if not reached:
            problems.append(f"届かない: {source.name} {source.url}")
    return problems


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def download(source: Source, folder: Path, *, opener: Opener = _open) -> str:
    """1 つ落として sha256 を返す 照合できる物は照合し、合わなければ消して止まる

    前に落とし終えた物が残っていれば落とし直さない qtwebengine は 500 MB を超えるので、
    1 つ落ちたあとに全部やり直すと時間が掛かる 最後まで書けて照合も通った物にしか
    本来の名前を付けないので、残っている物は完成品と見てよい 公開値と合わない物だけ
    落とし直す
    """
    target = folder / source.filename
    if target.exists():
        existing = _sha256(target)
        if source.sha256 is None or existing == source.sha256:
            return existing
    partial = target.with_name(target.name + ".part")
    try:
        with opener(_request(source.url)) as response, partial.open("wb") as stream:
            if not _is_archive(response):
                raise RuntimeError(f"{source.name}: アーカイブではなく HTML が返った")
            for block in iter(lambda: response.read(1 << 20), b""):
                stream.write(block)
        actual = _sha256(partial)
        if source.sha256 is not None and actual != source.sha256:
            raise RuntimeError(
                f"{source.name}: sha256 が上流の公開値と合わない（{actual} / {source.sha256}）"
            )
        # 書き終えて照合してから名前を付ける 途中で止まった物を完成品と取り違えない
        partial.replace(target)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return actual


def collect(sources: Sequence[Source], folder: Path, *, opener: Opener = _open) -> Path:
    """全部落とし、sha256 の一覧と manifest を書く 戻り値は manifest の場所"""
    folder.mkdir(parents=True, exist_ok=True)
    entries = []
    for source in sources:
        print(f"落とす: {source.name} {source.version}")
        digest = download(source, folder, opener=opener)
        entries.append(
            {
                "name": source.name,
                "version": source.version,
                "license": source.license,
                "url": source.url,
                "file": source.filename,
                "size": (folder / source.filename).stat().st_size,
                "sha256": digest,
                # 上流の公開値と照合できたか 照合していない物は落とした時点の値
                "verified_against_upstream": source.sha256 is not None,
            }
        )
    (folder / SUMS_NAME).write_text(
        "".join(f"{entry['sha256']}  {entry['file']}\n" for entry in entries), encoding="utf-8"
    )
    manifest = folder / MANIFEST_NAME
    manifest.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None, *, dist: Path | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="入手先に届くかだけ見る")
    args = parser.parse_args(argv)

    mismatches = installed_mismatches()
    for mismatch in mismatches:
        print(f"[NG] {mismatch}（tools/collect_sources.py の版を直す）")
    if mismatches:
        return 1

    if args.check:
        problems = check_urls(SOURCES)
        for problem in problems:
            print(f"[NG] {problem}")
        if not problems:
            print(f"[ok] {len(SOURCES)} 件すべてに届いた")
        return 1 if problems else 0

    folder = (dist if dist is not None else ROOT / "dist") / "sources"
    manifest = collect(SOURCES, folder)
    print(f"できた: {manifest.parent}（GitHub Release に zip と一緒に添付する）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
