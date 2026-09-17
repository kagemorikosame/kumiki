"""イージング 動きのエフェクト（シェーダの ``ease``）と場面切り替え（CPU 側）で同じ式を使う

YMM4 の ``EasingType`` と ``EasingMode`` の並びに合わせてある 番号はシェーダの ``ease`` と同じ
"""

from __future__ import annotations

import math

__all__ = ["EASING_KINDS", "EASING_MODES", "ease"]

#: イージングの種類 シェーダの ``ease`` の番号と同じ順
EASING_KINDS: tuple[tuple[str, str], ...] = (
    ("linear", "直線"),
    ("sine", "Sine"),
    ("quad", "Quad"),
    ("cubic", "Cubic"),
    ("quart", "Quart"),
    ("quint", "Quint"),
    ("expo", "Expo"),
    ("circ", "Circ"),
    ("back", "Back"),
    ("elastic", "Elastic"),
    ("bounce", "Bounce"),
)

#: イージングの向き
EASING_MODES: tuple[tuple[str, str], ...] = (("in", "In"), ("out", "Out"), ("inout", "InOut"))


def _bounce_out(t: float) -> float:
    if t < 1.0 / 2.75:
        return 7.5625 * t * t
    if t < 2.0 / 2.75:
        t -= 1.5 / 2.75
        return 7.5625 * t * t + 0.75
    if t < 2.5 / 2.75:
        t -= 2.25 / 2.75
        return 7.5625 * t * t + 0.9375
    t -= 2.625 / 2.75
    return 7.5625 * t * t + 0.984375


def _ease_in(t: float, kind: str) -> float:
    if kind == "sine":
        return 1.0 - math.cos(t * math.pi * 0.5)
    if kind in ("quad", "cubic", "quart", "quint"):
        return float(t ** {"quad": 2, "cubic": 3, "quart": 4, "quint": 5}[kind])
    if kind == "expo":
        return 0.0 if t <= 0.0 else math.pow(2.0, 10.0 * (t - 1.0))
    if kind == "circ":
        return 1.0 - math.sqrt(max(1.0 - t * t, 0.0))
    if kind == "back":
        return t * t * (2.70158 * t - 1.70158)
    if kind == "elastic":
        if t <= 0.0 or t >= 1.0:
            return t
        return -math.pow(2.0, 10.0 * (t - 1.0)) * math.sin((t - 1.075) * 2.0 * math.pi / 0.3)
    if kind == "bounce":
        return 1.0 - _bounce_out(1.0 - t)
    return t


def ease(t: float, kind: str = "linear", mode: str = "in") -> float:
    """0..1 の進み具合をイージングに通す 知らない種類は直線"""
    t = min(max(t, 0.0), 1.0)
    if mode == "out":
        return 1.0 - _ease_in(1.0 - t, kind)
    if mode == "inout":
        if t < 0.5:
            return _ease_in(t * 2.0, kind) * 0.5
        return 1.0 - _ease_in(2.0 - t * 2.0, kind) * 0.5
    return _ease_in(t, kind)
