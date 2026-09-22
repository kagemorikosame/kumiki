"""イージング 動きのエフェクト（シェーダの ``ease``）と場面切り替え（CPU 側）で同じ式を使う

YMM4 の ``EasingType`` と ``EasingMode`` の並びに合わせてある 番号はシェーダの ``ease`` と同じ
"""

from __future__ import annotations

# 式はコア層に置いた キーフレームの曲線（YMM4 の移動方法）も同じ式で動かすため
from sashimono.core.model.easing import ease

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
    #: 終わりまで動かず、終わりで一気に行き着く YMM4 の Jump（反復回転に描かせて確かめた）
    ("jump", "Jump"),
)

#: イージングの向き
EASING_MODES: tuple[tuple[str, str], ...] = (("in", "In"), ("out", "Out"), ("inout", "InOut"))
