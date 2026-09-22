"""エフェクトチェーンの実行

クリップ 1 本ぶんの絵に、エフェクトを順に掛ける 2 枚のフレームバッファを
交互に使い（ピンポン）、1 つのエフェクトの出力が次の入力になる

シェーダのコンパイルは初回だけ行い、以降は使い回す エフェクトを付け外しする
たびにコンパイルが走ると、パラメータをスライダーで動かしただけで固まる
"""

from __future__ import annotations

import logging
import math
from collections.abc import Collection
from dataclasses import dataclass

from OpenGL import GL

from kumiki.core.model import Effect, ParamValue
from kumiki.effects import EffectDefinition, registry
from kumiki.effects.spec import (
    CheckSpec,
    ColorSpec,
    FileSpec,
    SelectSpec,
    TrackSpec,
    ValueSpec,
)
from kumiki.engine.gpu.glutil import (
    FULL_RECT,
    VERTEX_SHADER,
    Framebuffer,
    Program,
    ScreenQuad,
    ShaderError,
    Texture,
)
from kumiki.engine.gpu.images import EffectImages

__all__ = ["EffectProcessor", "srgb_to_linear"]


_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _Compiled:
    """コンパイル済みのエフェクト"""

    definition: EffectDefinition
    program: Program


def srgb_to_linear(value: float) -> float:
    """sRGB の 0..1 をリニアへ

    色パラメータは sRGB で持っている（ユーザーが指定するのも画面に出るのも
    sRGB だから） シェーダはリニアで動くので、渡す直前に 1 度だけ変換する
    """
    if value <= 0.04045:
        return value / 12.92
    return float(((value + 0.055) / 1.055) ** 2.4)


class EffectProcessor:
    """エフェクトを順に適用する

    GL コンテキストが current な状態で使うこと ``quad`` は
    :class:`~kumiki.engine.gpu.Compositor` と共有する
    """

    def __init__(self, width: int, height: int, quad: ScreenQuad) -> None:
        self._quad = quad
        self._buffers = (Framebuffer(width, height), Framebuffer(width, height))
        #: エフェクトに入ってきた元の絵 ``u_source`` として渡す
        #: グローや影は「ぼかした結果」と「元の絵」の両方を要るので、
        #: ピンポンで上書きされる前に取っておく必要がある
        self._source = Framebuffer(width, height)
        self._blit = Program(VERTEX_SHADER, _BLIT_FRAGMENT)
        self._programs: dict[str, _Compiled | None] = {}
        #: エフェクトが読む画像（画像合成の絵、縁取りの模様）
        self._images = EffectImages()
        self._front = 0
        self._object: tuple[float, float, float, float] = (0.0, 0.0, float(width), float(height))
        self._duration = 0

    @property
    def width(self) -> int:
        return self._buffers[0].width

    @property
    def height(self) -> int:
        return self._buffers[0].height

    def resize(self, width: int, height: int) -> None:
        for buffer in (*self._buffers, self._source):
            buffer.resize(width, height)

    def has_work(self, effects: tuple[Effect, ...]) -> bool:
        """描画を伴うエフェクトが 1 つでもあるか

        無ければ中間バッファを経由せず、素材をそのまま合成できる エフェクトの
        無いクリップで全画面のパスが 1 回増えるのは、そのまま再生の余裕を削る
        """
        return any(self._compile(effect) is not None for effect in effects if effect.enabled)

    def apply(
        self,
        source: Texture | Framebuffer,
        effects: tuple[Effect, ...],
        *,
        frame: int,
        fps: float,
        source_rect: tuple[float, ...] = FULL_RECT,
        flip_source: bool = True,
        duration: int = 0,
        bounds: tuple[float, float, float, float] | None = None,
        premultiplied: bool = False,
    ) -> Framebuffer:
        """``source`` にエフェクトを掛けた結果のバッファを返す

        ``source_rect`` は、入力をバッファのどこに置くかをクリップ空間で指定する
        素材とプロジェクトの解像度が違うときに、ここで収める

        ``duration`` はクリップの長さ（フレーム） 登場と退場の動き（YMM4 の
        ``InOut*``）は終わりから逆算するので、長さを知らないと退場が始まらない

        ``bounds`` は絵の中身が実際にある範囲（画素、左・上・右・下、左上が原点）
        省くと ``source_rect`` 全体 テキストや図形は画面と同じ大きさの絵で届くので、
        全体を基準にすると角丸が画面の角に付き、中心基準の動きが画面の中央で回る
        """
        self._front = 0
        self._draw_source(source, source_rect, flip_source, premultiplied)
        width, height = float(self.width), float(self.height)
        #: 絵が置かれた範囲（画素、GL の向き） 角丸や中心基準の動きが使う
        if bounds is not None:
            left_px, top_px, right_px, bottom_px = bounds
            self._object = (left_px, height - bottom_px, right_px, height - top_px)
        else:
            left, bottom, right, top = (float(v) for v in source_rect[:4])
            self._object = (
                (left + 1.0) * 0.5 * width,
                (bottom + 1.0) * 0.5 * height,
                (right + 1.0) * 0.5 * width,
                (top + 1.0) * 0.5 * height,
            )
        self._duration = max(duration, 0)

        for effect in effects:
            if not effect.enabled:
                continue
            compiled = self._compile(effect)
            if compiled is None:
                continue
            self._apply_one(compiled, effect, frame=frame, fps=fps)

        return self._buffers[self._front]

    def release(self) -> None:
        for buffer in (*self._buffers, self._source):
            buffer.release()
        for compiled in self._programs.values():
            if compiled is not None:
                compiled.program.release()
        self._programs.clear()
        self._images.release()
        self._blit.release()

    # --- 内部 ---

    def _draw_source(
        self,
        source: Texture | Framebuffer,
        rect: tuple[float, ...],
        flip: bool,
        premultiplied: bool = False,
    ) -> None:
        """素材を先頭のバッファへ置く フレームバッファなら、その色のテクスチャを読む"""
        target = self._buffers[self._front]
        target.bind(clear=(0.0, 0.0, 0.0, 0.0))
        GL.glDisable(GL.GL_BLEND)
        self._blit.use()
        self._blit.set_vec4("u_rect", rect)
        self._blit.set_bool("u_flip", flip)
        # エフェクトはストレートアルファで受け取る 事前乗算で溜まった絵は戻してから置く
        self._blit.set_bool("u_premultiplied", premultiplied)
        handle = source.color if isinstance(source, Framebuffer) else source.handle
        self._blit.bind_texture("u_texture", handle)
        self._quad.draw()

    def _apply_one(self, compiled: _Compiled, effect: Effect, *, frame: int, fps: float) -> None:
        """エフェクト 1 つを、必要なパス数だけ掛ける"""
        definition = compiled.definition
        program = compiled.program

        # 元の絵を控えておく u_source を使うエフェクト（グロー・影）が要る
        self._copy(self._buffers[self._front], self._source)

        for index in range(definition.passes):
            source_buffer = self._buffers[self._front]
            target = self._buffers[1 - self._front]

            target.bind(clear=(0.0, 0.0, 0.0, 0.0))
            GL.glDisable(GL.GL_BLEND)
            program.use()
            program.set_vec4("u_rect", FULL_RECT)
            program.set_bool("u_flip", False)
            program.set_vec2("u_size", (float(target.width), float(target.height)))
            program.set_int("u_pass", index)
            program.set_float("u_frame", float(frame))
            program.set_float("u_time", float(frame) / fps if fps else 0.0)
            program.set_float("u_fps", fps)
            program.set_float("u_duration", float(self._duration) / fps if fps else 0.0)
            program.set_vec4("u_object", self._object)
            program.bind_texture("u_texture", source_buffer.color, unit=0)
            program.bind_texture("u_source", self._source.color, unit=1)
            self._set_parameters(program, definition, effect, frame)

            self._quad.draw()
            self._front = 1 - self._front

        self._grow_object(definition, effect, frame)

    def _grow_object(self, definition: EffectDefinition, effect: Effect, frame: int) -> None:
        """入れ物を広げるエフェクトの後で、絵の置かれた範囲を広げる

        広げないと、後ろに積んだミラーや角丸が**広げる前の範囲**で動く
        AviUtl の 領域拡張 → ミラー は、広げたぶんだけ鏡像が離れる並べ方
        """
        if definition.expands_object is None:
            return
        top, bottom, left, right = (
            _number(definition, effect, name, frame) for name in definition.expands_object
        )
        self._object = (
            self._object[0] - left,
            self._object[1] - bottom,
            self._object[2] + right,
            self._object[3] + top,
        )

    def _set_parameters(
        self, program: Program, definition: EffectDefinition, effect: Effect, frame: int
    ) -> None:
        """パラメータを uniform へ 名前はそのまま使う

        値は仕様の型へ寄せてから渡す ファイルから読んだ値は型までしか確かめて
        いないので、数のはずの所に文字が、整数の所に 32 ビットを超える数が入りうる
        そのまま渡すと GL の呼び出しが例外を投げ、プレビューも書き出しも止まる

        画像は 2 番のユニットから順に繋ぐ 0 と 1 は ``u_texture`` と ``u_source``
        """
        unit = 2
        for spec in definition.parameters:
            value: ParamValue | None = effect.params.get(spec.name)
            if value is None:
                value = spec.default_value()

            if isinstance(spec, TrackSpec):
                animated = spec.coerce(value)
                number = animated.at(frame)
                # 範囲では切らない 読み込んだテンプレートは表示の範囲を超える値を
                # 正しく使っていることがある 壊れた数（NaN や無限大）だけを既定へ戻す
                program.set_float(spec.name, number if math.isfinite(number) else spec.default)
            elif isinstance(spec, ColorSpec):
                color = spec.coerce(value)
                program.set_vec4(
                    spec.name,
                    (
                        srgb_to_linear(color[0]),
                        srgb_to_linear(color[1]),
                        srgb_to_linear(color[2]),
                        color[3],
                    ),
                )
            elif isinstance(spec, SelectSpec):
                program.set_int(spec.name, spec.index_of(spec.coerce(value)))
            elif isinstance(spec, CheckSpec):
                program.set_bool(spec.name, spec.coerce(value))
            elif isinstance(spec, ValueSpec):
                program.set_int(spec.name, spec.coerce(value))
            elif isinstance(spec, FileSpec) and spec.texture:
                unit = self._bind_image(program, spec, spec.coerce(value), unit)

    def _bind_image(self, program: Program, spec: FileSpec, path: str, unit: int) -> int:
        """画像を次の空いたテクスチャユニットへ繋ぐ 次に使うユニットを返す

        大きさは**毎回**渡す uniform の値はプログラムに残るので、画像を外した
        クリップが、前に描いた別のクリップの大きさを引き継いで空の画像を読む
        """
        texture = self._images.get(path)
        if texture is None:
            program.set_vec2(f"{spec.name}_size", (0.0, 0.0))
            return unit
        program.bind_texture(spec.name, texture.handle, unit=unit)
        program.set_vec2(f"{spec.name}_size", (float(texture.width), float(texture.height)))
        return unit + 1

    def retain_images(self, keep: Collection[str]) -> None:
        """``keep`` に無い画像を GPU から手放す GL が current な所で呼ぶこと"""
        self._images.retain(keep)

    def stale_images(self) -> frozenset[str]:
        """前に読んだときから書き換わった画像のパス GL は触らない"""
        return self._images.stale()

    def _copy(self, source: Framebuffer, target: Framebuffer) -> None:
        target.bind(clear=(0.0, 0.0, 0.0, 0.0))
        GL.glDisable(GL.GL_BLEND)
        self._blit.use()
        self._blit.set_bool("u_premultiplied", False)
        self._blit.set_vec4("u_rect", FULL_RECT)
        self._blit.set_bool("u_flip", False)
        self._blit.bind_texture("u_texture", source.color)
        self._quad.draw()

    def _compile(self, effect: Effect) -> _Compiled | None:
        """エフェクトのシェーダを用意する 描画を伴わないものは ``None``

        コンパイルに失敗したエフェクトも ``None`` を覚えて、二度と試さない
        毎フレーム失敗し続けると、ログが埋まるうえに描画が止まる
        """
        if effect.kind in self._programs:
            return self._programs[effect.kind]

        definition = registry.get(effect.kind)
        compiled: _Compiled | None = None
        if definition is not None and definition.fragment_shader is not None:
            try:
                compiled = _Compiled(
                    definition=definition,
                    program=Program(VERTEX_SHADER, definition.fragment_shader),
                )
            except ShaderError as error:
                # 1 度だけ残す 黙って捨てると、エフェクトが何も起きないまま
                # 「値の写し間違い」を探すことになる（実際に閃光で 1 度やった）
                _log.warning("エフェクト %s のシェーダを組めなかった: %s", effect.kind, error)
                compiled = None

        self._programs[effect.kind] = compiled
        return compiled


def _number(definition: EffectDefinition, effect: Effect, name: str, frame: int) -> float:
    """エフェクトの数の項目を 1 つ読む 読めなければ 0

    値の通し方は :meth:`EffectProcessor._set_parameters` と**同じにする**
    （``spec.coerce`` を通し、壊れた数は既定へ戻す） 別の読み方をすると、
    シェーダへ渡る値と入れ物を広げる量が食い違い、後ろのエフェクトだけずれる
    """
    spec = definition.spec(name)
    if not isinstance(spec, TrackSpec):
        return 0.0
    value = effect.params.get(name)
    number = spec.coerce(spec.default_value() if value is None else value).at(frame)
    return float(number) if math.isfinite(number) else float(spec.default)


_BLIT_FRAGMENT = """
#version 430 core
in vec2 v_uv;
out vec4 frag_color;
uniform sampler2D u_texture;
uniform bool u_premultiplied;
void main() {
    vec4 color = texture(u_texture, v_uv);
    if (u_premultiplied && color.a > 0.0001) color.rgb /= color.a;
    frag_color = color;
}
"""
