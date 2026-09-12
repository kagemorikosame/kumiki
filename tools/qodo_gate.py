"""Qodo のレビューが最新のコミットまで済んでいるかを、GitHub のチェックとして出す

Qodo は PR にコメントを書くだけで、GitHub のチェック（check run / status）を出さない
そのままでは main の保護で「必須」にできないので、ここで Qodo のコメントを読んで
``Qodo review`` という status を代わりに出す

    python tools/qodo_gate.py      （GitHub Actions の中で走らせる）

済んだとみなすのは次のどちらか

- Qodo のコメントに、PR の先頭のコミットの SHA が書かれている（Qodo は再レビューの
  たびに「updated up to the latest commit <URL>」というコメントを書き、指摘の根拠にも
  見たコミットの SHA 入りのリンクを貼る）
- 「Code Review by Qodo」のコメントが、先頭のコミットが GitHub に届いたあとに
  作られている（最初のレビューで指摘が無いと、SHA がどこにも書かれないことがある）

届いた時刻は、GitHub がそのコミットに check suite を作った時刻で見る コミットに
書かれた日時（committer date）は、手元で好きに作れる値なので使わない 使うと、
古い日時のコミットを後から push したとき、前のレビューで通ってしまう

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

#: Qodo のボットの名前 完全に一致するものだけを見る 前方一致にすると、
#: ``qodo-code-review-x`` のような一般のアカウントが SHA を書くだけで通せてしまう
#: ``[bot]`` の付く名前は GitHub App にしか付かないので、人には名乗れない
QODO_LOGIN = "qodo-code-review[bot]"

#: 最初のレビューの見出し 再レビューではこのコメント自体が書き換わる
REVIEW_TITLE = "Code Review by Qodo"


@dataclass(frozen=True, slots=True)
class Comment:
    login: str
    body: str
    created_at: datetime


def is_reviewed(head_sha: str, pushed_at: datetime | None, comments: list[Comment]) -> bool:
    """Qodo が ``head_sha`` まで見たか

    ``pushed_at`` は先頭のコミットが GitHub に届いた時刻 分からなければ ``None`` で、
    そのときは SHA が書かれているかだけで決める

    書き換えの時刻（updated_at）は見ない Qodo は指摘を「解決済み」にするときにも
    同じコメントを書き換えるので、古いコミットのレビューのまま新しく見える
    """
    for comment in comments:
        if comment.login != QODO_LOGIN:
            continue
        if head_sha in comment.body:
            return True
        first_review = REVIEW_TITLE in comment.body
        if first_review and pushed_at is not None and comment.created_at >= pushed_at:
            return True
    return False


def _pushed_at(repo: str, sha: str, token: str) -> datetime | None:
    """GitHub がそのコミットを受け取った時刻 check suite が作られた時刻で見る

    check suite は push を受けた GitHub が作るので、手元では作れない
    """
    suites = _api(f"repos/{repo}/commits/{sha}/check-suites", token).get("check_suites", [])
    times = [_parse_time(suite["created_at"]) for suite in suites]
    return min(times) if times else None


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
    pushed_at = _pushed_at(repo, head_sha, token)

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

    reviewed = is_reviewed(head_sha, pushed_at, comments)
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
