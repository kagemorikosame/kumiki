"""Qodo のレビューが最新のコミットまで済んでいるかを、GitHub のチェックとして出す

Qodo は PR にコメントを書くだけで、GitHub のチェック（check run / status）を出さない
そのままでは main の保護で「必須」にできないので、ここで Qodo のコメントを読んで
``Qodo review`` という status を代わりに出す

    python tools/qodo_gate.py      （GitHub Actions の中で走らせる）

済んだとみなすのは次のどちらか

- Qodo のコメントに、PR の先頭のコミットの SHA が書かれている（Qodo は再レビューの
  たびに「updated up to the latest commit <URL>」というコメントを書く）
- 「Code Review by Qodo」のコメントが、先頭のコミットより後に書かれている（最初の
  レビューには SHA が書かれない）

Qodo が止まったり無料枠が切れたりしたときの逃げ道は docs/development.md に書く
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

CONTEXT = "Qodo review"

#: Qodo のボットの名前 GitHub App のボットは ``<slug>[bot]`` になる
QODO_LOGIN_PREFIX = "qodo-code-review"

#: 最初のレビューの見出し 再レビューではこのコメント自体が書き換わる
REVIEW_TITLE = "Code Review by Qodo"


@dataclass(frozen=True, slots=True)
class Comment:
    login: str
    body: str
    created_at: datetime


def is_reviewed(head_sha: str, head_time: datetime, comments: list[Comment]) -> bool:
    """Qodo が ``head_sha`` まで見たか

    書き換えの時刻（updated_at）は見ない Qodo は指摘を「解決済み」にするときにも
    同じコメントを書き換えるので、古いコミットのレビューのまま新しく見える
    """
    for comment in comments:
        if not comment.login.startswith(QODO_LOGIN_PREFIX):
            continue
        if head_sha in comment.body:
            return True
        if REVIEW_TITLE in comment.body and comment.created_at >= head_time:
            return True
    return False


def _parse_time(text: str) -> datetime:
    # GitHub の時刻は末尾が Z Python 3.10 以前の fromisoformat は Z を読めないので置き換える
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _api(path: str, token: str, data: dict[str, str] | None = None) -> Any:
    request = urllib.request.Request(
        f"https://api.github.com/{path}",
        data=json.dumps(data).encode() if data is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        method="POST" if data is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def _pull_number(event: dict[str, Any]) -> int | None:
    if "pull_request" in event:
        return int(event["pull_request"]["number"])
    issue = event.get("issue") or {}
    # Issue のコメントでも走るので、PR のコメントだけに絞る
    if "pull_request" in issue:
        return int(issue["number"])
    return None


def main() -> int:
    token = os.environ["GITHUB_TOKEN"]
    repo = os.environ["GITHUB_REPOSITORY"]
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    number = _pull_number(event)
    if number is None:
        print("PR ではないので何もしない")
        return 0

    pull = _api(f"repos/{repo}/pulls/{number}", token)
    head_sha = pull["head"]["sha"]
    commit = _api(f"repos/{repo}/commits/{head_sha}", token)
    head_time = _parse_time(commit["commit"]["committer"]["date"])

    comments: list[Comment] = []
    page = 1
    while True:
        batch = _api(f"repos/{repo}/issues/{number}/comments?per_page=100&page={page}", token)
        comments += [
            Comment(c["user"]["login"], c["body"] or "", _parse_time(c["created_at"]))
            for c in batch
        ]
        if len(batch) < 100:
            break
        page += 1

    reviewed = is_reviewed(head_sha, head_time, comments)
    _api(
        f"repos/{repo}/statuses/{head_sha}",
        token,
        {
            "state": "success" if reviewed else "pending",
            "context": CONTEXT,
            "description": "最新のコミットまで Qodo が見た"
            if reviewed
            else "Qodo のレビュー待ち（PR に /agentic_review と書く）",
        },
    )
    print(f"{head_sha[:7]}: {'済み' if reviewed else '待ち'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
