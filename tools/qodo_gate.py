"""Qodo のレビューが最新のコミットまで済んでいるかを、GitHub のチェックとして出す

Qodo は PR にコメントを書くだけで、GitHub のチェック（check run / status）を出さない
そのままでは main の保護で「必須」にできないので、ここで Qodo のコメントを読んで
``Qodo review`` という status を代わりに出す

    python tools/qodo_gate.py      （GitHub Actions の中で走らせる）

済んだとみなすのは、Qodo のコメントに PR の先頭のコミットの SHA が書かれているときだけ
Qodo は再レビューのたびに「updated up to the latest commit <URL>」というコメントを書き、
指摘の根拠にも見たコミットの SHA 入りのリンクを貼る

時刻では判定しない 以前は「最初のレビューが push より後か」でも通していたが、
コミットの日時は手元で作れ、GitHub が受け取った時刻（check suite）も同じコミットが
別のブランチで先に push されていればその時刻になる どちらも、Qodo が見ていない
コミットを通す穴になった（PR #14 の Qodo のレビュー）

指摘の無い最初のレビューでは SHA がどこにも書かれないことがある そのときは
``/agentic_review`` で頼み直すか、docs/development.md の手順で手で通す
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CONTEXT = "Qodo review"

#: Qodo のボットの名前 完全に一致するものだけを見る 前方一致にすると、
#: ``qodo-code-review-x`` のような一般のアカウントが SHA を書くだけで通せてしまう
#: ``[bot]`` の付く名前は GitHub App にしか付かないので、人には名乗れない
QODO_LOGIN = "qodo-code-review[bot]"


@dataclass(frozen=True, slots=True)
class Comment:
    login: str
    body: str


def is_reviewed(head_sha: str, comments: list[Comment]) -> bool:
    """Qodo が ``head_sha`` まで見たか

    SHA は 40 桁のまま探す 短い形で探すと、別のコミットの SHA の一部に当たりうる
    """
    return any(c.login == QODO_LOGIN and head_sha in c.body for c in comments)


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

    head_sha = _api(f"repos/{repo}/pulls/{number}", token)["head"]["sha"]
    comments: list[Comment] = []
    page = 1
    while True:
        batch = _api(f"repos/{repo}/issues/{number}/comments?per_page=100&page={page}", token)
        comments += [Comment(c["user"]["login"], c["body"] or "") for c in batch]
        if len(batch) < 100:
            break
        page += 1

    reviewed = is_reviewed(head_sha, comments)
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
