"""ruff・mypy・pytest をまとめて走らせる

    .venv\\Scripts\\python.exe tools\\verify.py

1 つでも落ちたら終了コードが非 0 になる シェルでパイプに繋ぐと終了コードが
最後のコマンドのものに化けるので、判定はここで行う
"""

from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

if isinstance(sys.stdout, io.TextIOWrapper):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TARGETS = ["src", "tests", "tools"]

STEPS: list[tuple[str, list[str]]] = [
    ("ruff (書式)", ["-m", "ruff", "format", "--check", *TARGETS]),
    ("ruff (規約)", ["-m", "ruff", "check", *TARGETS]),
    ("mypy", ["-m", "mypy", "src", "tests"]),
    ("pytest", ["-m", "pytest", "-q"]),
    # 文章に句点を使わない約束 データとしての句点は見ない（tools/punctuation.py）
    ("句点", ["tools/punctuation.py"]),
]


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    failures: list[str] = []

    for name, arguments in STEPS:
        print(f"\n=== {name} ===", flush=True)
        completed = subprocess.run([sys.executable, *arguments], cwd=root, check=False)
        if completed.returncode != 0:
            failures.append(name)

    print()
    if failures:
        print("失敗: " + "、".join(failures))
        return 1
    print("すべて通過")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
