"""AviUtl スクリプトを描画の流れに組み込む

スクリプトは 1 枚の画像と描画パラメータを書き換える その結果は「何回・どこへ・
どう変形して描くか」の一覧（:class:`~kumiki.compat.aviutl.objapi.DrawCall`）に
なるので、レンダラはそれを順に合成すればよい

**Lua は Qt を知らない** テキストや図形を作る ``obj.load`` は
:func:`~kumiki.engine.sources.render_source` に委ねる 互換層をエンジンから
切り離しておくと、互換層だけをテストできる
"""

from __future__ import annotations

import numpy as np

from kumiki.compat.aviutl import PREFIX
from kumiki.compat.aviutl.catalog import ScriptCatalog, script_catalog
from kumiki.compat.aviutl.control import lua_value
from kumiki.compat.aviutl.objapi import DrawCall, EffectRequest, ObjectState
from kumiki.compat.aviutl.runtime import LuaScriptRuntime, blank_image
from kumiki.core.model import AnimatedValue, Clip, Effect, GeneratedSource, ParamValue
from kumiki.effects.definition import registry
from kumiki.engine.sources import render_source

__all__ = [
    "ScriptStage",
    "requested_effects",
    "script_catalog",
    "script_effects",
    "split_effects",
]


def split_effects(effects: tuple[Effect, ...]) -> tuple[tuple[Effect, ...], tuple[Effect, ...]]:
    """エフェクトを「GPU で処理するもの」と「スクリプト」に分ける"""
    gpu = tuple(e for e in effects if not e.kind.startswith(PREFIX))
    scripts = tuple(e for e in effects if e.kind.startswith(PREFIX))
    return gpu, scripts


def script_effects(effects: tuple[Effect, ...]) -> tuple[Effect, ...]:
    return tuple(e for e in effects if e.kind.startswith(PREFIX) and e.enabled)


class ScriptStage:
    """クリップに積まれた AviUtl スクリプトを走らせる係

    Lua ランタイムは 1 つを使い回す フレームごとに作り直すと、標準ライブラリの
    用意だけで描画の余裕を食う
    """

    def __init__(self, catalog: ScriptCatalog, *, screen: tuple[int, int]) -> None:
        self._catalog = catalog
        self._screen = screen
        self._runtime = LuaScriptRuntime(render_source=self._render_source)
        # 共通処理のファイル（.mod2）はスクリプトと同じ場所に置かれている
        self._runtime.set_roots(catalog.roots)

    def set_screen(self, width: int, height: int) -> None:
        self._screen = (width, height)

    def run(
        self,
        clip: Clip,
        effects: tuple[Effect, ...],
        image: np.ndarray,
        *,
        frame: int,
        fps: float,
        layer: int = 0,
    ) -> tuple[DrawCall, ...]:
        """スクリプトを順に走らせ、描画の一覧を返す

        1 つの状態を渡していくのは AviUtl と同じ 前のスクリプトが動かした位置に、
        次のスクリプトがさらに手を入れる形になる
        """
        state = ObjectState(
            image=image,
            screen_w=self._screen[0],
            screen_h=self._screen[1],
            frame=frame,
            totalframe=max(1, clip.duration),
            framerate=fps,
            layer=layer,
        )

        for effect in effects:
            entry = self._catalog.get(effect.kind)
            if entry is None:
                continue
            _apply_params(state, effect, frame)
            self._runtime.run(
                entry.source,
                state,
                header=entry.header,
                script=entry.label,
                folder=entry.folder,
            )

        return state.result()

    def expand_text(self, text: str, *, frame: int, fps: float, duration: int) -> str:
        """テキスト欄に埋め込んだ Lua を、そのフレームの文字にする"""
        state = ObjectState(
            image=blank_image(1, 1),
            screen_w=self._screen[0],
            screen_h=self._screen[1],
            frame=frame,
            totalframe=max(1, duration),
            framerate=fps,
        )
        return self._runtime.expand_text(text, state)

    def _render_source(
        self, kind: str, params: dict[str, object], width: int, height: int
    ) -> np.ndarray:
        """``obj.load`` から呼ばれる テキストや図形を絵にする"""
        source = GeneratedSource(kind=kind, params=_as_params(kind, params))
        drawn = render_source(source, max(1, width), max(1, height))
        if drawn is None:  # pragma: no cover - 種類は呼び出し側が決めている
            return np.zeros((max(1, height), max(1, width), 4), dtype=np.uint8)
        return drawn


def _apply_params(state: ObjectState, effect: Effect, frame: int) -> None:
    """設定欄の値を ``obj`` から見える形へ移す

    ``track0``…``track3`` と ``check0`` は AviUtl の決まった名前へ、それ以外は
    名前付きの値として置く スクリプトはどちらの書き方もする
    """
    definition = registry.get(effect.kind)
    specs = definition.parameters if definition is not None else ()

    state.track = [0.0, 0.0, 0.0, 0.0]
    state.values.clear()
    for spec in specs:
        raw = effect.params.get(spec.name, spec.default_value())
        value = raw.at(frame) if isinstance(raw, AnimatedValue) else raw
        state.values[spec.name] = lua_value(spec, value)

        if spec.name.startswith("track") and spec.name[5:].isdigit():
            slot = int(spec.name[5:])
            if slot < len(state.track):
                state.track[slot] = float(value)  # type: ignore[arg-type]
        elif spec.name == "check0":
            state.check0 = bool(value)


def _as_params(kind: str, values: dict[str, object]) -> dict[str, ParamValue]:
    """``obj.load`` の引数を、生成オブジェクトのパラメータへ

    数値は :class:`AnimatedValue` に包む 生成側はキーフレームを想定した形で
    値を読むため
    """
    del kind
    params: dict[str, ParamValue] = {}
    for name, value in values.items():
        if isinstance(value, str):
            params[name] = value
        elif isinstance(value, tuple):
            params[name] = tuple(float(part) for part in value)
        elif isinstance(value, bool):
            params[name] = int(value)
        elif isinstance(value, int | float):
            params[name] = AnimatedValue(float(value))
    return params


def requested_effects(call: DrawCall) -> tuple[Effect, ...]:
    """``obj.effect`` で頼まれたフィルタを、こちらのエフェクトへ"""
    return tuple(_to_effect(request) for request in call.effects)


def _to_effect(request: EffectRequest) -> Effect:
    definition = registry.get(request.kind)
    if definition is None:  # pragma: no cover - 対応表にある種別しか来ない
        return Effect(kind=request.kind, params={})

    params = definition.default_params()
    for name, value in request.params.items():
        spec = definition.spec(_translate(name))
        if spec is not None:
            params[spec.name] = spec.coerce(value)
    return Effect(kind=request.kind, params=params)


#: AviUtl のパラメータ名と、こちらの名前の対応
_PARAM_NAMES: dict[str, str] = {
    "範囲": "radius",
    "強さ": "strength",
    "しきい値": "threshold",
    "サイズ": "size",
    "幅": "width",
    "明るさ": "brightness",
    "コントラスト": "contrast",
    "色相": "hue",
    "彩度": "saturation",
    "輝度": "brightness",
    "X": "offset_x",
    "Y": "offset_y",
}


def _translate(name: str) -> str:
    return _PARAM_NAMES.get(name, name)
