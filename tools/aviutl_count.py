"""手元の AviUtl の配布物を全部通して、写せない所を数える

YMM4 側の ``tools/ymm4_probes.py`` と同じ考え方で、**実物に出る回数の多い順**に
埋めるための道具 推測で優先順位を決めると、実際には使われていないものから
手を付けることになる

既定で見に行くのは 2 か所

- ``%PROGRAMDATA%\\aviutl2\\Alias`` — AviUtl2 に登録されているエイリアス
- ``tests/fixtures/aviutl`` — 落としてきた配布物の置き場（リポジトリには入れない）

使い方::

    .venv\\Scripts\\python.exe tools\\aviutl_count.py
    .venv\\Scripts\\python.exe tools\\aviutl_count.py 置き場のパス --top 30
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kumiki.compat.aviutl.exo import ALIAS_SUFFIXES, ExoParseError, load_exo
from kumiki.compat.aviutl.mapping import map_object
from kumiki.compat.aviutl.report import CompatibilityReport
from kumiki.core.timebase import FrameRate


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

    rate = FrameRate(args.rate)
    missing: Counter[str] = Counter()
    broken: list[tuple[Path, str]] = []
    objects = 0
    animated = 0
    for path in targets:
        report = CompatibilityReport()
        try:
            document = load_exo(path)
        except (ExoParseError, OSError) as exc:
            broken.append((path, f"{type(exc).__name__}: {exc}"))
            continue
        for obj in document.objects:
            objects += 1
            if len(obj.points) > 2:
                animated += 1
            try:
                map_object(obj, rate, report=report)
            except Exception as exc:
                broken.append((path, f"{type(exc).__name__}: {exc}"))
        # 報告が数えた回数をそのまま足す 行を数え直すと、同じファイルの
        # 中で何度出ても 1 回になり、多い順の並びが崩れる
        missing.update(report.missing)

    print(f"オブジェクト {objects} 個 うち中間点を持つもの {animated} 個")
    print(f"読めなかったファイル {len(broken)} 本")
    for path, reason in broken[: args.top]:
        print(f"  {path.name}: {reason}")
    print(f"写せない所 {len(missing)} 種")
    for line, count in missing.most_common(args.top):
        print(f"  {count:4d}  {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
