"""GPU での映像合成。

色は必ずリニア空間で扱う。素材は sRGB で符号化されているので、テクスチャを
``GL_SRGB8_ALPHA8`` で作ってサンプリング時に自動でリニアへ戻し、合成を
``RGBA16F`` のフレームバッファ上で行い、最後に sRGB へ符号化して出す。

符号化されたままの値を足し引きすると、半透明の重ねやフェードが暗く沈む。
これは「なんとなく違う」ではなく明確に間違いなので、最初からリニアで通す。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from OpenGL import GL

__all__ = ["Compositor", "Placement", "Texture", "fit_placement"]


_VERTEX_SHADER = """
#version 430 core
layout(location = 0) in vec2 a_position;
out vec2 v_uv;
// 描画先の矩形。クリップ空間 (-1..1) で left, top, right, bottom。
uniform vec4 u_rect;
// 素材の上下反転。デコードした画像は左上が原点、GL は左下が原点。
uniform bool u_flip;
void main() {
    vec2 unit = a_position * 0.5 + 0.5;
    vec2 position = mix(u_rect.xy, u_rect.zw, unit);
    gl_Position = vec4(position, 0.0, 1.0);
    v_uv = vec2(unit.x, u_flip ? 1.0 - unit.y : unit.y);
}
"""

_FRAGMENT_SHADER = """
#version 430 core
in vec2 v_uv;
out vec4 frag_color;
uniform sampler2D u_texture;
uniform float u_opacity;
void main() {
    // sRGB テクスチャなので、この時点で値はリニア。
    vec4 color = texture(u_texture, v_uv);
    frag_color = vec4(color.rgb, color.a * u_opacity);
}
"""

_RESOLVE_FRAGMENT_SHADER = """
#version 430 core
in vec2 v_uv;
out vec4 frag_color;
uniform sampler2D u_texture;
uniform float u_opacity;

// リニアから sRGB への符号化。IEC 61966-2-1 の定義そのまま。
float encode(float c) {
    c = clamp(c, 0.0, 1.0);
    return c <= 0.0031308 ? c * 12.92 : 1.055 * pow(c, 1.0 / 2.4) - 0.055;
}

void main() {
    vec4 color = texture(u_texture, v_uv);
    frag_color = vec4(encode(color.r), encode(color.g), encode(color.b), color.a);
}
"""

_QUAD = np.array([-1.0, -1.0, 1.0, -1.0, -1.0, 1.0, 1.0, 1.0], dtype=np.float32)


@dataclass(frozen=True, slots=True)
class Placement:
    """描画先の矩形。ピクセル単位で、原点は左上。"""

    left: float
    top: float
    width: float
    height: float

    def to_clip(self, target_width: int, target_height: int) -> tuple[float, ...]:
        """クリップ空間 (-1..1) の ``(左, 上, 右, 下)`` へ。"""
        left = self.left / target_width * 2.0 - 1.0
        right = (self.left + self.width) / target_width * 2.0 - 1.0
        # GL の Y は下が正。上下を入れ替える。
        top = 1.0 - self.top / target_height * 2.0
        bottom = 1.0 - (self.top + self.height) / target_height * 2.0
        return (left, bottom, right, top)


def fit_placement(
    source_width: int, source_height: int, target_width: int, target_height: int
) -> Placement:
    """縦横比を保ったまま画面に収まる配置を返す。

    Premiere の「フレームサイズに合わせる」と同じ振る舞い。素材とプロジェクトの
    解像度が違うときに、引き伸ばして歪ませるより収める方が驚きが少ない。
    """
    if source_width <= 0 or source_height <= 0:
        return Placement(0.0, 0.0, float(target_width), float(target_height))

    scale = min(target_width / source_width, target_height / source_height)
    width = source_width * scale
    height = source_height * scale
    return Placement(
        left=(target_width - width) / 2.0,
        top=(target_height - height) / 2.0,
        width=width,
        height=height,
    )


class Texture:
    """GPU 上の 1 枚の画像。

    ``GL_SRGB8_ALPHA8`` で作るので、シェーダから読んだ時点でリニアになっている。
    """

    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.handle = int(GL.glGenTextures(1))
        GL.glBindTexture(GL.GL_TEXTURE_2D, self.handle)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_LINEAR)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_LINEAR)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_S, GL.GL_CLAMP_TO_EDGE)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_T, GL.GL_CLAMP_TO_EDGE)
        GL.glTexImage2D(
            GL.GL_TEXTURE_2D,
            0,
            GL.GL_SRGB8_ALPHA8,
            width,
            height,
            0,
            GL.GL_RGBA,
            GL.GL_UNSIGNED_BYTE,
            None,
        )
        GL.glBindTexture(GL.GL_TEXTURE_2D, 0)

    @classmethod
    def from_array(cls, image: np.ndarray) -> Texture:
        """``(高さ, 幅, 4)`` の uint8 配列からテクスチャを作る。"""
        height, width = image.shape[:2]
        texture = cls(width, height)
        texture.upload(image)
        return texture

    def upload(self, image: np.ndarray) -> None:
        """画素を書き込む。大きさが違えば作り直す。"""
        if image.ndim != 3 or image.shape[2] != 4 or image.dtype != np.uint8:
            raise ValueError(f"RGBA uint8 の配列が必要: shape={image.shape}, dtype={image.dtype}")

        height, width = image.shape[:2]
        GL.glBindTexture(GL.GL_TEXTURE_2D, self.handle)
        GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, 1)
        data = np.ascontiguousarray(image)
        if (width, height) != (self.width, self.height):
            GL.glTexImage2D(
                GL.GL_TEXTURE_2D,
                0,
                GL.GL_SRGB8_ALPHA8,
                width,
                height,
                0,
                GL.GL_RGBA,
                GL.GL_UNSIGNED_BYTE,
                data,
            )
            self.width, self.height = width, height
        else:
            GL.glTexSubImage2D(
                GL.GL_TEXTURE_2D,
                0,
                0,
                0,
                width,
                height,
                GL.GL_RGBA,
                GL.GL_UNSIGNED_BYTE,
                data,
            )
        GL.glBindTexture(GL.GL_TEXTURE_2D, 0)

    def release(self) -> None:
        if self.handle:
            GL.glDeleteTextures([self.handle])
            self.handle = 0


class Compositor:
    """トラックを重ねて 1 枚の絵にする。

    GL コンテキストが current な状態で使うこと。生成も描画も解放もすべて同じ
    コンテキスト上で行う必要がある。
    """

    def __init__(self, width: int, height: int) -> None:
        if width <= 0 or height <= 0:
            raise ValueError(f"解像度が不正: {width}x{height}")
        self.width = width
        self.height = height

        self._program = _build_program(_VERTEX_SHADER, _FRAGMENT_SHADER)
        self._resolve_program = _build_program(_VERTEX_SHADER, _RESOLVE_FRAGMENT_SHADER)
        self._vao, self._vbo = _build_quad()
        self._fbo, self._color = _build_framebuffer(width, height, GL.GL_RGBA16F)
        # 読み出し用。リニアの合成結果を sRGB へ符号化して受け取る。
        self._resolve_fbo, self._resolve_color = _build_framebuffer(width, height, GL.GL_RGBA8)

    def resize(self, width: int, height: int) -> None:
        if (width, height) == (self.width, self.height):
            return
        if width <= 0 or height <= 0:
            raise ValueError(f"解像度が不正: {width}x{height}")
        self._release_framebuffers()
        self.width, self.height = width, height
        self._fbo, self._color = _build_framebuffer(width, height, GL.GL_RGBA16F)
        self._resolve_fbo, self._resolve_color = _build_framebuffer(width, height, GL.GL_RGBA8)

    def begin(self, background: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 1.0)) -> None:
        """合成を始める。背景色はリニア値で指定する。"""
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self._fbo)
        GL.glViewport(0, 0, self.width, self.height)
        GL.glClearColor(*background)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT)
        GL.glEnable(GL.GL_BLEND)
        # ストレートアルファ。素材は非乗算済みなので、これが正しい合成式。
        GL.glBlendFuncSeparate(
            GL.GL_SRC_ALPHA, GL.GL_ONE_MINUS_SRC_ALPHA, GL.GL_ONE, GL.GL_ONE_MINUS_SRC_ALPHA
        )

    def draw(
        self,
        texture: Texture,
        *,
        placement: Placement | None = None,
        opacity: float = 1.0,
        flip: bool = True,
    ) -> None:
        """1 枚を重ねる。

        ``flip`` の既定が真なのは、デコードした画像が左上原点で、GL のテクスチャ座標が
        左下原点だから。素材由来の画像はほぼ常に反転が要る。
        """
        if placement is None:
            placement = fit_placement(texture.width, texture.height, self.width, self.height)

        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self._fbo)
        GL.glViewport(0, 0, self.width, self.height)
        GL.glUseProgram(self._program)
        GL.glUniform4f(
            GL.glGetUniformLocation(self._program, "u_rect"),
            *placement.to_clip(self.width, self.height),
        )
        GL.glUniform1i(GL.glGetUniformLocation(self._program, "u_flip"), int(flip))
        GL.glUniform1f(
            GL.glGetUniformLocation(self._program, "u_opacity"), float(np.clip(opacity, 0.0, 1.0))
        )
        GL.glActiveTexture(GL.GL_TEXTURE0)
        GL.glBindTexture(GL.GL_TEXTURE_2D, texture.handle)
        GL.glUniform1i(GL.glGetUniformLocation(self._program, "u_texture"), 0)

        GL.glBindVertexArray(self._vao)
        GL.glDrawArrays(GL.GL_TRIANGLE_STRIP, 0, 4)
        GL.glBindVertexArray(0)

    def read(self) -> np.ndarray:
        """合成結果を sRGB 符号化した ``(高さ, 幅, 4)`` の uint8 配列で返す。"""
        # リニアの結果をもう 1 パス通して sRGB へ符号化する。glReadPixels に
        # RGBA16F から直接 uint8 で読ませると、変換式が実装依存になる。
        self._resolve(self._resolve_fbo, (0, 0, self.width, self.height))

        GL.glPixelStorei(GL.GL_PACK_ALIGNMENT, 1)
        raw = GL.glReadPixels(0, 0, self.width, self.height, GL.GL_RGBA, GL.GL_UNSIGNED_BYTE)
        image = np.frombuffer(raw, dtype=np.uint8).reshape(self.height, self.width, 4)
        # GL は左下原点で返すので、画像として扱えるよう上下を戻す。
        return np.ascontiguousarray(image[::-1])

    def present(
        self,
        framebuffer: int,
        viewport: tuple[int, int, int, int],
        *,
        letterbox: bool = True,
    ) -> None:
        """合成結果を画面（や任意のフレームバッファ）へ直接出す。

        プレビュー表示用。:meth:`read` と違って CPU へ戻さないので、GPU から
        CPU へ、また GPU へ、という往復が要らない。1080p なら 1 フレームあたり
        8MB の転送が消える。

        ``letterbox`` が真なら、``viewport`` の中で縦横比を保って収める。
        プレビュー枠の形が映像と違っても歪まない。
        """
        x, y, width, height = viewport
        if width <= 0 or height <= 0:
            return

        target = viewport
        if letterbox:
            placed = fit_placement(self.width, self.height, width, height)
            target = (
                x + int(placed.left),
                y + int(placed.top),
                max(1, int(placed.width)),
                max(1, int(placed.height)),
            )

        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, framebuffer)
        GL.glViewport(x, y, width, height)
        GL.glDisable(GL.GL_BLEND)
        GL.glClearColor(0.0, 0.0, 0.0, 1.0)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT)
        self._resolve(framebuffer, target)

    def _resolve(self, framebuffer: int, viewport: tuple[int, int, int, int]) -> None:
        """リニアの合成結果を sRGB へ符号化して ``framebuffer`` へ描く。"""
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, framebuffer)
        GL.glViewport(*viewport)
        GL.glDisable(GL.GL_BLEND)
        GL.glUseProgram(self._resolve_program)
        GL.glUniform4f(
            GL.glGetUniformLocation(self._resolve_program, "u_rect"), -1.0, -1.0, 1.0, 1.0
        )
        GL.glUniform1i(GL.glGetUniformLocation(self._resolve_program, "u_flip"), 0)
        GL.glActiveTexture(GL.GL_TEXTURE0)
        GL.glBindTexture(GL.GL_TEXTURE_2D, self._color)
        GL.glUniform1i(GL.glGetUniformLocation(self._resolve_program, "u_texture"), 0)
        GL.glBindVertexArray(self._vao)
        GL.glDrawArrays(GL.GL_TRIANGLE_STRIP, 0, 4)
        GL.glBindVertexArray(0)

    def release(self) -> None:
        self._release_framebuffers()
        GL.glDeleteVertexArrays(1, [self._vao])
        GL.glDeleteBuffers(1, [self._vbo])
        GL.glDeleteProgram(self._program)
        GL.glDeleteProgram(self._resolve_program)

    def _release_framebuffers(self) -> None:
        GL.glDeleteFramebuffers(2, [self._fbo, self._resolve_fbo])
        GL.glDeleteTextures([self._color, self._resolve_color])


def _build_program(vertex_source: str, fragment_source: str) -> int:
    vertex = _compile_shader(GL.GL_VERTEX_SHADER, vertex_source)
    fragment = _compile_shader(GL.GL_FRAGMENT_SHADER, fragment_source)
    program = int(GL.glCreateProgram())
    GL.glAttachShader(program, vertex)
    GL.glAttachShader(program, fragment)
    GL.glLinkProgram(program)
    if not GL.glGetProgramiv(program, GL.GL_LINK_STATUS):
        log = GL.glGetProgramInfoLog(program)
        raise RuntimeError(f"シェーダのリンクに失敗: {_decode(log)}")
    GL.glDeleteShader(vertex)
    GL.glDeleteShader(fragment)
    return program


def _compile_shader(kind: int, source: str) -> int:
    shader = int(GL.glCreateShader(kind))
    GL.glShaderSource(shader, source)
    GL.glCompileShader(shader)
    if not GL.glGetShaderiv(shader, GL.GL_COMPILE_STATUS):
        log = GL.glGetShaderInfoLog(shader)
        raise RuntimeError(f"シェーダのコンパイルに失敗: {_decode(log)}\n{source}")
    return shader


def _build_quad() -> tuple[int, int]:
    vao = int(GL.glGenVertexArrays(1))
    vbo = int(GL.glGenBuffers(1))
    GL.glBindVertexArray(vao)
    GL.glBindBuffer(GL.GL_ARRAY_BUFFER, vbo)
    GL.glBufferData(GL.GL_ARRAY_BUFFER, _QUAD.nbytes, _QUAD, GL.GL_STATIC_DRAW)
    GL.glEnableVertexAttribArray(0)
    GL.glVertexAttribPointer(0, 2, GL.GL_FLOAT, GL.GL_FALSE, 0, None)
    GL.glBindVertexArray(0)
    return vao, vbo


def _build_framebuffer(width: int, height: int, internal_format: int) -> tuple[int, int]:
    color = int(GL.glGenTextures(1))
    GL.glBindTexture(GL.GL_TEXTURE_2D, color)
    GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_LINEAR)
    GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_LINEAR)
    GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_S, GL.GL_CLAMP_TO_EDGE)
    GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_T, GL.GL_CLAMP_TO_EDGE)
    GL.glTexImage2D(
        GL.GL_TEXTURE_2D,
        0,
        internal_format,
        width,
        height,
        0,
        GL.GL_RGBA,
        GL.GL_FLOAT if internal_format == GL.GL_RGBA16F else GL.GL_UNSIGNED_BYTE,
        None,
    )

    fbo = int(GL.glGenFramebuffers(1))
    GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, fbo)
    GL.glFramebufferTexture2D(
        GL.GL_FRAMEBUFFER, GL.GL_COLOR_ATTACHMENT0, GL.GL_TEXTURE_2D, color, 0
    )
    status = GL.glCheckFramebufferStatus(GL.GL_FRAMEBUFFER)
    if status != GL.GL_FRAMEBUFFER_COMPLETE:
        raise RuntimeError(f"フレームバッファを構成できない: 0x{status:x}")
    GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, 0)
    return fbo, color


def _decode(log: bytes | str) -> str:
    return log.decode("utf-8", "replace") if isinstance(log, bytes) else str(log)
