"""スクリプトが ``obj.effect`` で積んだ効果を、その場で絵へ掛ける（Issue #176）

AviUtl の ``obj.effect`` は呼んだ所で絵を変える こちらの効果は GPU のシェーダなので、
ふだんは積んでおいて描くときにまとめて掛ける 間に ``obj.effect("リサイズ")`` や
``obj.copybuffer`` のような絵を読む・変える呼び出しが挟まったときだけ、ここで 1 枚の
絵に掛けて読み戻す（:meth:`~sashimono.compat.aviutl.objapi.ObjApi._settle_effects`）

絵は**画面の画素**のまま扱う スクリプトは画質に関わらずプロジェクトの解像度で動くので
（:meth:`FrameRenderer._draw_scripted`）、画素で決める設定も等倍で掛ける
"""

from __future__ import annotations

import numpy as np

from sashimono.core.model import Effect
from sashimono.engine.gpu import Compositor, EffectProcessor, Placement, Texture

__all__ = ["BAKE_MARGIN", "ScriptEffectBaker"]

#: 絵の周りに空ける余白（画素） ぼかしや影は絵の外へ広がる AviUtl のぼかしも絵を
#: 広げるので、広がった所まで残す 効果の設定から広がりを読む決まりは無いので、
#: こちらの効果の上限（ぼかし・グローの範囲 96、影のずれ）が収まる幅にする
BAKE_MARGIN = 128


class ScriptEffectBaker:
    """効果を掛けて読み戻す係 GL のコンテキストが current な所で使う

    合成先と効果の作業場は、描画の物とは別に持つ 大きさが絵ごとに違ううえ、
    描画の作業場を使うと、描いている途中の絵を壊す 使うまで作らない
    """

    def __init__(self) -> None:
        self._compositor: Compositor | None = None
        self._effects: EffectProcessor | None = None
        self._texture: Texture | None = None

    def apply(
        self,
        image: np.ndarray,
        effects: tuple[Effect, ...],
        frame: int,
        fps: float,
        duration: int,
    ) -> np.ndarray:
        """``image`` へ ``effects`` を掛けた絵 広がった所まで含め、真ん中は動かさない

        戻す絵は、余白のうち透明なままの所を左右（上下）同じ幅だけ削った物
        同じ幅で削るのは、絵の真ん中がオブジェクトの位置だから 片側だけ削ると、
        効果を掛けただけで絵が横へずれる 元の絵の内側までは削らない 透明な縁を持つ絵が、
        効果を掛けただけで小さくなる
        """
        height, width = image.shape[:2]
        canvas_w, canvas_h = width + 2 * BAKE_MARGIN, height + 2 * BAKE_MARGIN
        compositor, processor, texture = self._prepared(canvas_w, canvas_h)
        if not processor.has_work(effects):
            return image

        texture.upload(image)
        placed = Placement(float(BAKE_MARGIN), float(BAKE_MARGIN), float(width), float(height))
        result = processor.apply(
            texture,
            effects,
            frame=frame,
            fps=fps,
            source_rect=placed.to_clip(canvas_w, canvas_h),
            duration=duration,
            bounds=(
                float(BAKE_MARGIN),
                float(BAKE_MARGIN),
                float(BAKE_MARGIN + width),
                float(BAKE_MARGIN + height),
            ),
        )
        compositor.begin((0.0, 0.0, 0.0, 0.0))
        compositor.draw_handle(
            result.color,
            Placement(0.0, 0.0, float(canvas_w), float(canvas_h)),
            flip=False,
        )
        # スクリプトの絵はストレートアルファ 事前乗算のまま返すと、描くときに不透明度が
        # もう 1 度掛かって、ぼけた縁が暗くなる
        baked = compositor.read(straight=True)
        return _trimmed(baked, BAKE_MARGIN)

    def release(self) -> None:
        if self._compositor is not None:
            self._compositor.release()
        if self._effects is not None:
            self._effects.release()
        if self._texture is not None:
            self._texture.release()
        self._compositor = self._effects = self._texture = None

    def _prepared(self, width: int, height: int) -> tuple[Compositor, EffectProcessor, Texture]:
        if self._compositor is None or self._effects is None or self._texture is None:
            self._compositor = Compositor(width, height)
            self._effects = EffectProcessor(width, height, self._compositor.quad)
            self._texture = Texture(1, 1)
        elif (self._compositor.width, self._compositor.height) != (width, height):
            self._compositor.resize(width, height)
            self._effects.resize(width, height)
        return self._compositor, self._effects, self._texture


def _trimmed(image: np.ndarray, margin: int) -> np.ndarray:
    """余白の透明な所を、左右・上下それぞれ同じ幅だけ削る ``margin`` より多くは削らない"""
    alpha = image[..., 3] > 0
    height, width = alpha.shape
    rows = np.flatnonzero(alpha.any(axis=1))
    columns = np.flatnonzero(alpha.any(axis=0))
    if rows.size == 0:
        # 何も残らない絵 元の大きさの透明な絵にする 大きさまで消すと ``obj.w`` が 0 になる
        return np.ascontiguousarray(image[margin : height - margin, margin : width - margin])
    top = min(int(rows[0]), height - 1 - int(rows[-1]), margin)
    left = min(int(columns[0]), width - 1 - int(columns[-1]), margin)
    return np.ascontiguousarray(image[top : height - top, left : width - left])
