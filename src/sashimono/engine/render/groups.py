"""グループ制御（AviUtl の拡張編集のグループ制御）を、受け持つクリップへ当てる

グループ制御は自分では何も描かない 手前の N 本のトラック（:func:`controlling_groups`）の、
同じ時刻に描くクリップ 1 本ずつへ、次の 3 つを当てる

- 配置（位置・拡大・回転）と反転 クリップ自身の配置の**後ろ**へ足す 配置の支点は画面の中央
  なので、クリップの位置はグループの位置を中心に拡大・回転され、グループの位置だけずれる
  AviUtl のグループ制御の座標・拡大率・回転と同じ（グループの中心を基準に各オブジェクトへ掛ける）
- 不透明度 クリップの不透明度へ掛ける
- エフェクト（足した物） クリップ自身のエフェクトの後ろ、クリップの配置の前へ足す
  AviUtl のグループ制御のフィルタ効果は、対象の各オブジェクトに 1 つずつ掛かる
  （まとめて 1 枚にしてから掛けるフィルタオブジェクトとの違い）

まとめて 1 枚に描いてから動かす形にしなかったのは、AviUtl が 1 本ずつに掛けるから
1 枚にすると、グループの中でぼかしを掛けたときに隣り合う物の境目がにじみ合い、合成モードや
上のクリップの切り抜き（``clip_to_below``）の相手も変わる

グループ制御の値はグループ制御の時刻で読み、その時刻の止まった値にして当てる
クリップの時刻で読むと、グループ制御の頭とクリップの頭がずれているときに動きがずれる
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from sashimono.core.commands.fixed import loose_slot
from sashimono.core.model import AnimatedValue, Clip, Effect, ParamValue, Track

__all__ = ["grouped"]


def grouped(clip: Clip, groups: Sequence[tuple[Track, Clip]], frame: int) -> Clip:
    """``groups``（近い物から :func:`~sashimono.core.model.controlling_groups`）を当てたクリップ

    描くときだけに使う プロジェクトには残さない
    """
    if not groups:
        return clip
    loose: list[Effect] = []
    placements: list[Effect] = []
    factor = 1.0
    for _, group in groups:
        local = frame - group.timeline_start
        for effect in group.effects:
            if not effect.enabled:
                continue
            frozen = replace(effect, params=_frozen(effect.params, local))
            # 反転と配置は固定の欄のまま足す 部分フィルタの範囲は固定の欄の手前で閉じるので、
            # ふつうのエフェクトとして足すと、クリップの部分フィルタの中でだけ動く
            (placements if effect.fixed else loose).append(frozen)
        factor *= group.opacity.at(local)
    effects = list(clip.effects)
    at = loose_slot(effects)
    effects[at:at] = loose
    # 近いグループから足す 内側のグループで動かした物を、外側のグループがさらに動かす
    effects.extend(placements)
    return replace(clip, effects=tuple(effects), opacity=_scaled(clip.opacity, factor))


def _frozen(params: dict[str, ParamValue], local: int) -> dict[str, ParamValue]:
    return {
        name: AnimatedValue(value.at(local)) if isinstance(value, AnimatedValue) else value
        for name, value in params.items()
    }


def _scaled(value: AnimatedValue, factor: float) -> AnimatedValue:
    """不透明度に ``factor`` を掛ける キーフレームは形を保ったまま（残像が前の時刻を読むため）"""
    if factor == 1.0:
        return value
    return AnimatedValue(
        static=value.static * factor,
        keyframes=tuple(replace(k, value=k.value * factor) for k in value.keyframes),
    )
