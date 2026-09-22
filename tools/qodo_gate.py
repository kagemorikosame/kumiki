"""Qodo のレビューが最新のコミットまで済んでいるかを、GitHub のチェックとして出す

Qodo は PR にコメントを書くだけで、GitHub のチェック（check run / status）を出さない
そのままでは main の保護で「必須」にできないので、ここで Qodo のコメントを読んで
``Qodo review`` という status を代わりに出す

    python tools/qodo_gate.py      （GitHub Actions の中で走らせる）

済んだとみなすのは、Qodo のコメントに PR の先頭のコミットの SHA が書かれているとき
Qodo は再レビューのたびに「updated up to the latest commit <URL>」というコメントを書き、
指摘の根拠にも見たコミットの SHA 入りのリンクを貼る

コミットの時刻では判定しない 以前は「最初のレビューが push より後か」でも通していたが、
コミットの日時は手元で作れ、GitHub が受け取った時刻（check suite）も同じコミットが
別のブランチで先に push されていればその時刻になる どちらも、Qodo が見ていない
コミットを通す穴になった（PR #14 の Qodo のレビュー）

PR の向き先（base）を変えると、先頭の SHA は同じまま比べる相手が変わり、Qodo が見た
差分とは別の差分になる 向き先を変えたあとは、そのあとに書かれた Qodo のコメントだけを
数える 変えた時刻は GitHub が PR の履歴に記録するもの（base_ref_changed）を使う
向き先が進んだだけのときは、main の保護の「最新にしてからマージ」で先頭の SHA が変わる

main の保護は「最新の main を取り込んでからマージ」なので、ほかの PR が入るたびに
先頭の SHA が変わる Qodo は中身が同じ差分を見直さず「No code changes since the last
review」と返すだけなので、SHA だけで見ると取り込みのたびに待ちへ戻って止まる
そこで、**取り込んだだけなら通す**（:func:`only_base_merges_since`）
中身が変わっていないことは機械で確かめる 人の判断は挟まない

指摘の無い最初のレビューでは SHA がどこにも書かれないことがある そのときは
``/agentic_review`` で頼み直すか、docs/development.md の手順で手で通す
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
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
    created_at: datetime


def is_reviewed(head_sha: str, comments: list[Comment], since: datetime | None = None) -> bool:
    """Qodo が ``head_sha`` まで見たか

    ``since`` は向き先を最後に変えた時刻 それより前のコメントは、別の差分を見たものなので
    数えない SHA は 40 桁のまま探す 短い形で探すと、別のコミットの SHA の一部に当たりうる
    """
    return any(
        c.login == QODO_LOGIN and head_sha in c.body and (since is None or c.created_at > since)
        for c in comments
    )


#: 取り込みを辿る深さの上限 1 つの PR で何十回も取り込むことはない 上限を置かないと、
#: 作られたコミットの連なりを延々と辿って API を叩き続けることになる
MAX_MERGE_WALK = 20

#: 取り込みで変わってよいファイルの数の上限 これを超えたら辿らずに待ちにする
#: 数が多いほど照合の API 呼び出しが増えるので、諦めて人の目に回す
MAX_MERGED_FILES = 300


def only_base_merges_since(
    head_sha: str,
    base: str,
    reviewed: Callable[[str], bool],
    api: Callable[[str], Any],
    depth: int = MAX_MERGE_WALK,
) -> str | None:
    """``head_sha`` が「Qodo の見たコミットへ main を取り込んだだけ」なら、その SHA

    Qodo は中身の同じ差分を見直さないので、main を取り込んで先頭が変わるたびに
    待ちへ戻ってしまう 取り込みだけなら Qodo の見た差分のままなので通してよい

    通すのは次を全部満たすときだけ 取り込みに見せかけて中身を変えた PR は通さない

    * 先頭から親をたどる道が、すべて**マージコミット**であること
      （自分のコミットが 1 つでもあれば、Qodo が見ていない中身が入っている）
    * 取り込んだ相手（2 つ目以降の親）が**すでに main に入っている**こと
    * Qodo の見たコミットから先頭までに**変わったファイルが、どれも main 側と同じ中身**
      であること（衝突を手で直すと、どちらとも違う中身になるので通らない）
    """
    sha = head_sha
    for _ in range(depth):
        parents: list[str] = [str(p["sha"]) for p in api(f"commits/{sha}")["parents"]]
        if len(parents) < 2:
            # 自分のコミット ここから先は Qodo の見た差分ではない
            return None
        if not all(_is_in_base(parent, base, api) for parent in parents[1:]):
            return None
        merged_from = parents[1]
        sha = parents[0]
        if reviewed(sha):
            # Qodo の見たコミットに着いた ここで中身が合わなければ、取り込み以外の
            # 手が入っている さらに古いコミットを探しても中身は合わないので止める
            return sha if _matches_base(sha, head_sha, merged_from, api) else None
    return None


def _is_in_base(sha: str, base: str, api: Callable[[str], Any]) -> bool:
    """``sha`` が ``base`` ブランチに入っているか

    ``identical`` は同じコミット ``behind`` は base の方が先にある、つまり ``sha`` は
    base の祖先 それ以外は main に入っていない物を取り込んだということなので通さない
    """
    return api(f"compare/{sha}...{base}")["status"] in ("identical", "behind")


def _matches_base(
    reviewed_sha: str, head_sha: str, merged_from: str, api: Callable[[str], Any]
) -> bool:
    """見たコミットから先頭までに変わったファイルが、どれも main 側と同じ中身か

    衝突を手で直したときは、どちらの側とも違う中身になるのでここで落ちる
    ファイルの中身は blob の SHA で比べる 中身が 1 バイトでも違えば別の SHA になる
    """
    changed = api(f"compare/{reviewed_sha}...{head_sha}").get("files") or []
    if len(changed) > MAX_MERGED_FILES:
        return False
    from_base = {
        f["filename"]: f["sha"]
        for f in (api(f"compare/{reviewed_sha}...{merged_from}").get("files") or [])
    }
    return all(from_base.get(f["filename"]) == f["sha"] for f in changed)


def last_base_change(events: list[dict[str, Any]]) -> datetime | None:
    """PR の履歴から、向き先を最後に変えた時刻 変えていなければ ``None``"""
    times = [_parse_time(e["created_at"]) for e in events if e.get("event") == "base_ref_changed"]
    return max(times) if times else None


def _parse_time(text: str) -> datetime:
    # GitHub の時刻は末尾が Z 古い Python の fromisoformat は Z を読めないので置き換える
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


def _all_pages(path: str, token: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    page = 1
    while True:
        batch = _api(f"{path}?per_page=100&page={page}", token)
        items += batch
        if len(batch) < 100:
            return items
        page += 1


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
    base = pull["base"]["ref"]
    since = last_base_change(_all_pages(f"repos/{repo}/issues/{number}/timeline", token))
    comments = [
        Comment(c["user"]["login"], c["body"] or "", _parse_time(c["created_at"]))
        for c in _all_pages(f"repos/{repo}/issues/{number}/comments", token)
    ]

    def reviewed_at(sha: str) -> bool:
        return is_reviewed(sha, comments, since)

    merged_from = None
    if reviewed_at(head_sha):
        description = "最新のコミットまで Qodo が見た"
    else:
        # Qodo は中身の同じ差分を見直さない 取り込むたびに待ちへ戻ると、ほかの PR が
        # 入るだけでマージできなくなる
        merged_from = only_base_merges_since(
            head_sha, base, reviewed_at, lambda path: _api(f"repos/{repo}/{path}", token)
        )
        description = (
            f"{base} を取り込んだだけ Qodo は {merged_from[:7]} まで見た" if merged_from else ""
        )

    reviewed = reviewed_at(head_sha) or merged_from is not None
    _api(
        f"repos/{repo}/statuses/{head_sha}",
        token,
        {
            "state": "success" if reviewed else "pending",
            "context": CONTEXT,
            "description": description or "Qodo のレビュー待ち（PR に /agentic_review と書く）",
        },
    )
    seen = f"（{merged_from[:7]} の差分）" if merged_from else ""
    print(f"{head_sha[:7]}: {'済み' if reviewed else '待ち'}{seen}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
