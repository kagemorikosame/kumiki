"""Mutation testing の結果が既存の検出力を下回っていないか確かめる"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TypedDict, cast


class _Summary(TypedDict):
    total: int
    zapped: int
    survived: int
    timeout: int
    error: int


REPORT = Path("coverage/gremlins/gremlins.json")
# 最初の導入時に確認した値 既存の弱い所は許すが、これより検出力を落とさない
MAX_SURVIVORS = 19
MINIMUM_PERCENTAGE = 87.7


def _load_summary(path: Path) -> _Summary:
    data = cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))
    return cast(_Summary, data["summary"])


def main() -> int:
    try:
        summary = _load_summary(REPORT)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        print(f"mutation report を読めない: {exc}", file=sys.stderr)
        return 1

    if summary["total"] <= 0:
        print("mutation が 1 件も生成されていない", file=sys.stderr)
        return 1
    percentage = summary["zapped"] * 100.0 / summary["total"]
    print(
        "mutation score: "
        f"{percentage:.1f}% "
        f"({summary['zapped']}/{summary['total']} zapped, {summary['survived']} survived)"
    )
    if summary["timeout"] or summary["error"]:
        print("mutation の実行に timeout または error がある", file=sys.stderr)
        return 1
    if summary["survived"] > MAX_SURVIVORS:
        print(
            f"生き残った mutation が基準の {MAX_SURVIVORS} 件を超えた",
            file=sys.stderr,
        )
        return 1
    if percentage < MINIMUM_PERCENTAGE:
        print(
            f"mutation score が基準の {MINIMUM_PERCENTAGE:.1f}% を下回った",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
