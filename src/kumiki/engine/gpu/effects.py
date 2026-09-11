"""エフェクトチェーンの実行。

クリップ 1 本ぶんの絵に、エフェクトを順に掛ける。2 枚のフレームバッファを
交互に使い（ピンポン）、1 つのエフェクトの出力が次の入力になる。

シェーダのコンパイルは初回だけ行い、以降は使い回す。エフェクトを付け外しする
たびにコンパイルが走ると、パラメータをスライダーで動かしただけで固まる。
"""

from __future__ import annotations

from dataclasses import dataclass

from OpenGL import GL

from kumiki.core.model import AnimatedValue, Effect, ParamValue
from kumiki.effects import EffectDefinition, registry
from kumiki.effects.spec import ColorSpec, SelectSpec
from kumiki.engine.gpu.glutil import (
    FULL_RECT,
    VERTEX_SHADER,
    Framebuffer,
    Program,
    ScreenQuad,
    ShaderError,
    Texture,
)

__all__ = ["EffectProcessor", "srgb_to_linear"]


@dataclass(frozen=True, slots=True)
class _Compiled:
    """コンパイル済みのエフェクト。"""

    definition: EffectDefinition
    program: Program


def srgb_to_linear(value: float) -> float:
    """sRGB の 0..1 をリニアへ。

    色パラメータは sRGB で持っている（ユーザーが指定するのも画面に出るのも
    sRGB だから）。シェーダはリニアで動くので、渡す直前に 1 度だけ変換する。
    """
    if value <= 0.04045:
        return value / 12.92
    return float(((value + 0.055) / 1.055) ** 2.4)


class EffectProcessor:
    """エフェクトを順に適用する。

    GL コンテキストが current な状態で使うこと。``quad`` は
    :class:`~kumiki.engine.gpu.Compositor` と共有する。
    """

    def __init__(self, width: int, height: int, quad: ScreenQuad) -> None:
        self._quad = quad
        self._buffers = (Framebuffer(width, height), Framebuffer(width, height))
        #: エフェクトに入ってきた元の絵。``u_source`` として渡す。
        #: グローや影は「ぼかした結果」と「元の絵」の両方を要るので、
        #: ピンポンで上書きされる前に取っておく必要がある。
        self._source = Framebuffer(width, height)
        self._blit = Program(VERTEX_SHADER, _BLIT_FRAGMENT)
        self._programs: dict[str, _Compiled | None] = {}
        self._front = 0

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
        """描画を伴うエフェクトが 1 つでもあるか。

        無ければ中間バッファを経由せず、素材をそのまま合成できる。エフェクトの
        無いクリップで全画面のパスが 1 回増えるのは、そのまま再生の余裕を削る。
        """
        return any(self._compile(effect) is not None for effect in effects if effect.enabled)

    def apply(
        self,
        source: Texture,
        effects: tuple[Effect, ...],
        *,
        frame: int,
        fps: float,
        source_rect: tuple[float, ...] = FULL_RECT,
        flip_source: bool = True,
    ) -> Framebuffer:
        """``source`` にエフェクトを掛けた結果のバッファを返す。

        ``source_rect`` は、入力をバッファのどこに置くかをクリップ空間で指定する。
        素材とプロジェクトの解像度が違うときに、ここで収める。
        """
        self._front = 0
        self._draw_source(source, source_rect, flip_source)

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
        self._blit.release()

    # --- 内部 ---

    def _draw_source(self, source: Texture, rect: tuple[float, ...], flip: bool) -> None:
        """素材を先頭のバッファへ置く。"""
        target = self._buffers[self._front]
        target.bind(clear=(0.0, 0.0, 0.0, 0.0))
        GL.glDisable(GL.GL_BLEND)
        self._blit.use()
        self._blit.set_vec4("u_rect", rect)
        self._blit.set_bool("u_flip", flip)
        self._blit.bind_texture("u_texture", source.handle)
        self._quad.draw()

    def _apply_one(self, compiled: _Compiled, effect: Effect, *, frame: int, fps: float) -> None:
        """エフェクト 1 つを、必要なパス数だけ掛ける。"""
        definition = compiled.definition
        program = compiled.program

        # 元の絵を控えておく。u_source を使うエフェクト（グロー・影）が要る。
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
            program.bind_texture("u_texture", source_buffer.color, unit=0)
            program.bind_texture("u_source", self._source.color, unit=1)
            self._set_parameters(program, definition, effect, frame)

            self._quad.draw()
            self._front = 1 - self._front

    def _set_parameters(
        self, program: Program, definition: EffectDefinition, effect: Effect, frame: int
    ) -> None:
        """パラメータを uniform へ。名前はそのまま使う。"""
        for spec in definition.parameters:
            value: ParamValue | None = effect.params.get(spec.name)
            if value is None:
                value = spec.default_value()

            if isinstance(value, AnimatedValue):
                program.set_float(spec.name, value.at(frame))
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
            elif isinstance(value, bool):
                program.set_bool(spec.name, value)
            elif isinstance(value, int | float):
                program.set_int(spec.name, int(value))

    def _copy(self, source: Framebuffer, target: Framebuffer) -> None:
        target.bind(clear=(0.0, 0.0, 0.0, 0.0))
        GL.glDisable(GL.GL_BLEND)
        self._blit.use()
        self._blit.set_vec4("u_rect", FULL_RECT)
        self._blit.set_bool("u_flip", False)
        self._blit.bind_texture("u_texture", source.color)
        self._quad.draw()

    def _compile(self, effect: Effect) -> _Compiled | None:
        """エフェクトのシェーダを用意する。描画を伴わないものは ``None``。

        コンパイルに失敗したエフェクトも ``None`` を覚えて、二度と試さない。
        毎フレーム失敗し続けると、ログが埋まるうえに描画が止まる。
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
            except ShaderError:
                compiled = None

        self._programs[effect.kind] = compiled
        return compiled


_BLIT_FRAGMENT = """
#version 430 core
in vec2 v_uv;
out vec4 frag_color;
uniform sampler2D u_texture;
void main() {
    frag_color = texture(u_texture, v_uv);
}
"""
