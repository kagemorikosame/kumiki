"""アシスタントで選べるモデルと、考える深さ（エフォート）

一覧はネットに取りに行かず、ここに定数で持つ 画面を開くたびに問い合わせると、
繋がっていない機械ではモデルを選ぶ所から先へ進めない

どちらも「空文字 = 指定しない」を既定にする 指定しないと Claude Code 自身の
既定（ログインしたアカウントに合わせて決まる）が使われ、これまでの動きと変わらない
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "DEFAULT_EFFORT",
    "DEFAULT_MODEL",
    "EFFORTS",
    "MODELS",
    "EffortChoice",
    "ModelChoice",
    "effort_for",
    "find_model",
]


@dataclass(frozen=True, slots=True)
class ModelChoice:
    """選べるモデル 1 つ"""

    #: API のモデル ID 空なら Claude Code の既定に任せる
    id: str
    label: str
    #: エフォートを受け付けるか Haiku 4.5 は受け付けず、渡すと失敗する
    effort: bool = True


@dataclass(frozen=True, slots=True)
class EffortChoice:
    """考える深さ 1 段"""

    #: ``claude --effort`` に渡す値 空なら渡さない
    value: str
    label: str


MODELS: tuple[ModelChoice, ...] = (
    ModelChoice("", "既定（Claude Code に任せる）"),
    ModelChoice("claude-opus-5-5", "Claude Opus 5.5"),
    ModelChoice("claude-sonnet-5", "Claude Sonnet 5"),
    ModelChoice("claude-haiku-4-5-20251001", "Claude Haiku 4.5", effort=False),
    ModelChoice("claude-fable-5-1", "Claude Fable 5.1"),
)

#: 値は claude-agent-sdk 0.2.152 の ``EffortLevel`` と同じ 5 段
#: SDK はこれを ``claude --effort`` へそのまま渡す
EFFORTS: tuple[EffortChoice, ...] = (
    EffortChoice("", "既定"),
    EffortChoice("low", "低（速い・安い）"),
    EffortChoice("medium", "中"),
    EffortChoice("high", "高"),
    EffortChoice("xhigh", "とても高い"),
    EffortChoice("max", "最大（遅い・高い）"),
)

DEFAULT_MODEL = MODELS[0].id
DEFAULT_EFFORT = EFFORTS[0].value


def find_model(model_id: str) -> ModelChoice | None:
    return next((choice for choice in MODELS if choice.id == model_id), None)


def effort_for(model_id: str, effort: str) -> str | None:
    """そのモデルへ実際に渡すエフォート 渡さないなら ``None``

    受け付けないモデルへ渡すと、会話が始まる前に失敗する 画面で選んだまま
    モデルだけ Haiku に替えた人が、理由の分からない失敗を見ないようにする
    """
    if not effort:
        return None
    choice = find_model(model_id)
    if choice is not None and not choice.effort:
        return None
    return effort
