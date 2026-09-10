"""YMM4 の文字装飾（``Decorations``）を、テキストオブジェクトの設定へ。

YMM4 はテキストの飾りを**列**で持つ。下から順に重なるので、縁取りを 2 本重ねて
二重の縁にする、といった書き方ができる。

こちらのテキストオブジェクトが自前で持てる飾りは縁取りと影の 1 つずつなので、

* 縁取りは**一番太いもの**をテキストに載せ、残りは :mod:`縁取りエフェクト
  <novaedit.effects.builtin>` として外側に積む
* 影は最初の 1 つをテキストに載せる

という分け方をする。エフェクトとして積んだ縁取りは文字の外形に沿って付くので、
重ね順も YMM4 と同じ「内側から外側へ」になる。

**注意**: この対応付けは、手元に YMM4 が入っていないため実ファイルでの
突き合わせができていない。振り分けはクラス名だけで行い、知らない装飾は
:mod:`~novaedit.compat.aviutl.report` に残すので、合わない装飾が来ても
読み込み自体は通る。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from novaedit.compat.aviutl.report import CompatibilityReport
from novaedit.compat.ymm4.values import animated, colour, number, type_name
from novaedit.core.model import AnimatedValue, Effect, ParamValue
from novaedit.effects.definition import registry

__all__ = ["DecorationResult", "map_decorations"]


@dataclass(slots=True)
class DecorationResult:
    """装飾を分けた結果。"""

    #: テキストオブジェクトへ直接載せる設定。
    params: dict[str, ParamValue] = field(default_factory=dict)
    #: 外側に積むエフェクト。内側から外側の順。
    effects: list[Effect] = field(default_factory=list)


def map_decorations(
    decorations: Any, report: CompatibilityReport, *, size: float = 64.0
) -> DecorationResult:
    """``Decorations`` の列を読む。列でなければ何もしない。"""
    result = DecorationResult()
    if not isinstance(decorations, list):
        return result

    borders: list[tuple[float, tuple[float, float, float, float]]] = []
    for entry in decorations:
        if not isinstance(entry, dict):
            continue
        name = type_name(entry)
        if name.endswith("BorderDecoration") or name == "Border":
            borders.append(_border(entry))
        elif name.endswith("ShadowDecoration") or name == "Shadow":
            _shadow(entry, result, size)
        elif name.endswith("GradationDecoration") or name.endswith("GradientDecoration"):
            _gradient(entry, result)
        elif name.endswith("BlurDecoration"):
            _blur(entry, result)
        else:
            report.note_missing(f"YMM4 の装飾: {name or '種類不明'}")

    _place_borders(borders, result)
    return result


def _border(entry: dict[str, Any]) -> tuple[float, tuple[float, float, float, float]]:
    thickness = number(entry.get("Thickness"), number(entry.get("Width"), 4.0))
    return max(0.0, thickness), colour(entry.get("Color"), (0.0, 0.0, 0.0, 1.0))


def _place_borders(
    borders: list[tuple[float, tuple[float, float, float, float]]],
    result: DecorationResult,
) -> None:
    """縁取りを、テキスト側 1 本とエフェクト側の残りに分ける。

    一番太いものをテキストに持たせるのは、それが文字の形をいちばん強く決めるから。
    細いほうをテキストに載せると、太いほうをエフェクトで足したときに二重の縁の
    間隔が変わる。
    """
    if not borders:
        return
    widest = max(borders, key=lambda item: item[0])
    result.params["border_width"] = AnimatedValue(widest[0])
    result.params["border_color"] = widest[1]

    definition = registry.get("border")
    if definition is None:  # pragma: no cover - 標準エフェクトは必ずある
        return
    for thickness, tint in borders:
        if (thickness, tint) == widest:
            continue
        result.effects.append(definition.create(width=thickness, color=tint))


def _shadow(entry: dict[str, Any], result: DecorationResult, size: float) -> None:
    if "shadow_x" in result.params:
        # 2 つ目以降の影は載せられない。1 つ目だけを使い、残りは黙って捨てず
        # エフェクトの影として積む。
        definition = registry.get("shadow")
        if definition is not None:
            result.effects.append(
                definition.create(
                    offset_x=number(entry.get("X"), 0.0),
                    offset_y=number(entry.get("Y"), 0.0),
                    blur=number(entry.get("Blur"), 0.0),
                    color=colour(entry.get("Color"), (0.0, 0.0, 0.0, 1.0)),
                )
            )
        return

    default = size * 0.06
    result.params["shadow_x"] = animated(entry.get("X"), default)
    result.params["shadow_y"] = animated(entry.get("Y"), -default)
    result.params["shadow_blur"] = animated(entry.get("Blur"), 0.0)
    result.params["shadow_color"] = colour(entry.get("Color"), (0.0, 0.0, 0.0, 1.0))


def _gradient(entry: dict[str, Any], result: DecorationResult) -> None:
    definition = registry.get("gradient")
    if definition is None:  # pragma: no cover - 標準エフェクトは必ずある
        return
    colours = entry.get("Colors")
    start = (1.0, 1.0, 1.0, 1.0)
    end = (0.0, 0.0, 0.0, 1.0)
    if isinstance(colours, list) and len(colours) >= 2:
        start = colour(colours[0], start)
        end = colour(colours[-1], end)
    result.effects.append(
        definition.create(
            start_color=start,
            end_color=end,
            angle=number(entry.get("Angle"), 90.0),
            span=number(entry.get("Length"), 100.0),
        )
    )


def _blur(entry: dict[str, Any], result: DecorationResult) -> None:
    definition = registry.get("blur")
    if definition is None:  # pragma: no cover - 標準エフェクトは必ずある
        return
    result.effects.append(definition.create(radius=number(entry.get("Size"), 4.0)))
