"""手元の AviUtl の配布物を全部通して、写せない所を数える

YMM4 側の ``tools/ymm4_probes.py`` と同じ考え方で、**実物に出る回数の多い順**に
埋めるための道具 推測で優先順位を決めると、実際には使われていないものから
手を付けることになる

既定で見に行くのは 2 か所

- ``%PROGRAMDATA%\\aviutl2\\Alias`` — AviUtl2 に登録されているエイリアス
- ``tests/fixtures/aviutl`` — 落としてきた配布物の置き場（リポジトリには入れない）

拡張子ごと（``.exa`` は AviUtl1、``.object`` は AviUtl2）にも分けて数える 世代で
書き方が違うので、まとめた数だけでは、どちらの読み手の穴なのかが分からない

エイリアスが呼ぶスクリプト（``.anm`` ``.obj`` など）は、いつもの置き場に加えて
探す場所の中からも探す 配布物はスクリプトとエイリアスを 1 つの zip で配るので、
隣に置いてあるものを読まないと、アニメーション効果がどれも「見つからない」になる

使い方::

    .venv\\Scripts\\python.exe tools\\aviutl_count.py
    .venv\\Scripts\\python.exe tools\\aviutl_count.py 置き場のパス --top 30
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sashimono.compat.aviutl.catalog import (
    ScriptCatalog,
    default_script_roots,
    set_script_catalog,
)
from sashimono.compat.aviutl.exo import ALIAS_SUFFIXES, ExoParseError, load_exo
from sashimono.compat.aviutl.mapping import map_object
from sashimono.compat.aviutl.report import CompatibilityReport
from sashimono.core.timebase import FrameRate


def default_roots() -> list[Path]:
    """探しに行く場所 無ければ黙って飛ばす"""
    roots = []
    program_data = os.environ.get("PROGRAMDATA")
    if program_data:
        roots.append(Path(program_data) / "aviutl2" / "Alias")
    roots.append(Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "aviutl")
    return [root for root in roots if root.is_dir()]


def files_under(roots: list[Path]) -> list[Path]:
    found: list[Path] = []
    for root in roots:
        for suffix in ALIAS_SUFFIXES:
            found.extend(sorted(root.rglob(f"*{suffix}")))
    return sorted(set(found))


@dataclass
class Tally:
    """1 つの拡張子の集計"""

    files: int = 0
    objects: int = 0
    #: 中間点を持つオブジェクト
    animated: int = 0
    #: 何も置かずに終わったオブジェクト 読めても 0 個なら、読み手が節の名前を知らない
    empty_files: int = 0
    broken: list[tuple[Path, str]] = field(default_factory=list)
    missing: Counter[str] = field(default_factory=Counter)


def count(targets: list[Path], rate: FrameRate) -> dict[str, Tally]:
    """ファイルを全部通して、拡張子ごとに数える キーの ``*`` は全体"""
    tallies: dict[str, Tally] = {}
    for path in targets:
        for key in ("*", path.suffix.lower()):
            tallies.setdefault(key, Tally())
        _count_one(path, rate, (tallies["*"], tallies[path.suffix.lower()]))
    return tallies


def _count_one(path: Path, rate: FrameRate, into: tuple[Tally, ...]) -> None:
    report = CompatibilityReport()
    for tally in into:
        tally.files += 1
    try:
        document = load_exo(path)
    except (ExoParseError, OSError) as exc:
        for tally in into:
            tally.broken.append((path, f"{type(exc).__name__}: {exc}"))
        return
    if not document.objects:
        # 開けても何も置かないファイルは、読めなかったのと同じ 以前の AviUtl1 の
        # .exa はどれもここに落ちていたのに、読めなかった数は 0 本と出ていた
        for tally in into:
            tally.empty_files += 1
    for obj in document.objects:
        for tally in into:
            tally.objects += 1
            if len(obj.points) > 2:
                tally.animated += 1
        try:
            map_object(obj, rate, report=report)
        except Exception as exc:
            for tally in into:
                tally.broken.append((path, f"{type(exc).__name__}: {exc}"))
    # 報告が数えた回数をそのまま足す 行を数え直すと、同じファイルの
    # 中で何度出ても 1 回になり、多い順の並びが崩れる
    for tally in into:
        tally.missing.update(report.missing)


def use_scripts_beside(roots: list[Path]) -> None:
    """いつもの置き場に加えて、探す場所の中のスクリプトも読めるようにする"""
    catalog = ScriptCatalog(roots=(*default_script_roots(), *roots))
    catalog.scan()
    set_script_catalog(catalog)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roots", nargs="*", type=Path, help="探す場所 省略すると既定の 2 か所")
    parser.add_argument("--top", type=int, default=40, help="多い順に何件まで出すか")
    parser.add_argument("--rate", type=int, default=60, help="フレームレート 区間の換算に使う")
    args = parser.parse_args()

    roots = [Path(root) for root in args.roots] or default_roots()
    if not roots:
        print("探す場所が見つからない 配布物を tests/fixtures/aviutl へ置く")
        return 1

    targets = files_under(roots)
    print(f"{len(targets)} 本を読む（{', '.join(str(root) for root in roots)}）")
    use_scripts_beside(roots)
    tallies = count(targets, FrameRate(args.rate))

    print("拡張子ごと")
    print("  拡張子    本数  オブジェクト  中間点  何も置かない  読めない  写せない所（種/回）")
    for suffix in sorted(key for key in tallies if key != "*"):
        tally = tallies[suffix]
        print(
            f"  {suffix:8s} {tally.files:5d} {tally.objects:13d} {tally.animated:7d}"
            f" {tally.empty_files:13d} {len(tally.broken):9d}"
            f"  {len(tally.missing)}/{sum(tally.missing.values())}"
        )

    for suffix in sorted(tallies, key=lambda key: (key != "*", key)):
        tally = tallies[suffix]
        title = "全体" if suffix == "*" else suffix
        print(f"[{title}] オブジェクト {tally.objects} 個 うち中間点を持つもの {tally.animated} 個")
        print(f"[{title}] 読めなかったファイル {len(tally.broken)} 本")
        for path, reason in tally.broken[: args.top]:
            print(f"  {path.name}: {reason}")
        print(f"[{title}] 写せない所 {len(tally.missing)} 種")
        for line, times in tally.missing.most_common(args.top):
            print(f"  {times:4d}  {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
