"""OpenGL の下回り。シェーダ、フレームバッファ、テクスチャ、全画面四角形。

合成もエフェクトも同じ道具を使う。ここに集めておかないと、エフェクトを足すたびに
似たような GL 呼び出しの塊が増えていく。

どれも「GL コンテキストが current な状態で使う」という前提を共有する。
生成・使用・解放をすべて同じコンテキスト上で行うこと。
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
from OpenGL import GL

__all__ = ["VERTEX_SHADER", "Framebuffer", "Program", "ScreenQuad", "Texture"]

#: 全画面四角形を描く頂点シェーダ。フラグメント側だけ差し替えれば、どのエフェクトも
#: 同じ形で書ける。``u_rect`` で描画先の矩形を、``u_flip`` で上下反転を指定する。
VERTEX_SHADER = """
#version 430 core
layout(location = 0) in vec2 a_position;
out vec2 v_uv;
// 描画先の矩形。クリップ空間 (-1..1) で left, bottom, right, top。
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

_QUAD = np.array([-1.0, -1.0, 1.0, -1.0, -1.0, 1.0, 1.0, 1.0], dtype=np.float32)

#: 描画先いっぱいに広げる矩形。エフェクトのように全面を塗るときに使う。
FULL_RECT = (-1.0, -1.0, 1.0, 1.0)


class Program:
    """コンパイル済みのシェーダ。

    uniform の位置を初回に引いて覚える。描画のたびに ``glGetUniformLocation`` を
    呼ぶと、エフェクトを重ねたときに無視できない回数の問い合わせになる。
    """

    def __init__(self, vertex_source: str, fragment_source: str) -> None:
        vertex = _compile(GL.GL_VERTEX_SHADER, vertex_source)
        fragment = _compile(GL.GL_FRAGMENT_SHADER, fragment_source)

        self.handle = int(GL.glCreateProgram())
        GL.glAttachShader(self.handle, vertex)
        GL.glAttachShader(self.handle, fragment)
        GL.glLinkProgram(self.handle)
        if not GL.glGetProgramiv(self.handle, GL.GL_LINK_STATUS):
            log = _decode(GL.glGetProgramInfoLog(self.handle))
            raise ShaderError(f"シェーダのリンクに失敗: {log}")

        GL.glDeleteShader(vertex)
        GL.glDeleteShader(fragment)
        self._locations: dict[str, int] = {}

    def use(self) -> None:
        GL.glUseProgram(self.handle)

    def location(self, name: str) -> int:
        """uniform の位置。無い名前は -1 で、GL 側が黙って無視する。"""
        cached = self._locations.get(name)
        if cached is None:
            cached = int(GL.glGetUniformLocation(self.handle, name))
            self._locations[name] = cached
        return cached

    def set_float(self, name: str, value: float) -> None:
        GL.glUniform1f(self.location(name), float(value))

    def set_int(self, name: str, value: int) -> None:
        GL.glUniform1i(self.location(name), int(value))

    def set_bool(self, name: str, value: bool) -> None:
        GL.glUniform1i(self.location(name), 1 if value else 0)

    def set_vec2(self, name: str, values: Sequence[float]) -> None:
        GL.glUniform2f(self.location(name), *(float(v) for v in values[:2]))

    def set_vec4(self, name: str, values: Sequence[float]) -> None:
        GL.glUniform4f(self.location(name), *(float(v) for v in values[:4]))

    def bind_texture(self, name: str, handle: int, unit: int = 0) -> None:
        GL.glActiveTexture(GL.GL_TEXTURE0 + unit)
        GL.glBindTexture(GL.GL_TEXTURE_2D, handle)
        self.set_int(name, unit)

    def release(self) -> None:
        if self.handle:
            GL.glDeleteProgram(self.handle)
            self.handle = 0


class ShaderError(RuntimeError):
    """シェーダをコンパイルまたはリンクできない。"""


class ScreenQuad:
    """全画面四角形。すべての描画がこれ 1 つを使い回す。"""

    def __init__(self) -> None:
        self._vao = int(GL.glGenVertexArrays(1))
        self._vbo = int(GL.glGenBuffers(1))
        GL.glBindVertexArray(self._vao)
        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, self._vbo)
        GL.glBufferData(GL.GL_ARRAY_BUFFER, _QUAD.nbytes, _QUAD, GL.GL_STATIC_DRAW)
        GL.glEnableVertexAttribArray(0)
        GL.glVertexAttribPointer(0, 2, GL.GL_FLOAT, GL.GL_FALSE, 0, None)
        GL.glBindVertexArray(0)

    def draw(self) -> None:
        GL.glBindVertexArray(self._vao)
        GL.glDrawArrays(GL.GL_TRIANGLE_STRIP, 0, 4)
        GL.glBindVertexArray(0)

    def release(self) -> None:
        if self._vao:
            GL.glDeleteVertexArrays(1, [self._vao])
            GL.glDeleteBuffers(1, [self._vbo])
            self._vao = 0
            self._vbo = 0


class Texture:
    """GPU 上の 1 枚の画像。

    ``srgb`` が真なら ``GL_SRGB8_ALPHA8`` で作る。素材は sRGB で符号化されて
    いるので、シェーダから読んだ時点でリニアに戻っていてほしい。エフェクトの
    途中結果はすでにリニアなので、そちらは偽にする。
    """

    def __init__(self, width: int, height: int, *, srgb: bool = True) -> None:
        self.width = max(1, width)
        self.height = max(1, height)
        self._internal_format = GL.GL_SRGB8_ALPHA8 if srgb else GL.GL_RGBA8
        self.handle = int(GL.glGenTextures(1))

        GL.glBindTexture(GL.GL_TEXTURE_2D, self.handle)
        _set_sampling()
        GL.glTexImage2D(
            GL.GL_TEXTURE_2D,
            0,
            self._internal_format,
            self.width,
            self.height,
            0,
            GL.GL_RGBA,
            GL.GL_UNSIGNED_BYTE,
            None,
        )
        GL.glBindTexture(GL.GL_TEXTURE_2D, 0)

    @classmethod
    def from_array(cls, image: np.ndarray, *, srgb: bool = True) -> Texture:
        """``(高さ, 幅, 4)`` の uint8 配列からテクスチャを作る。"""
        height, width = image.shape[:2]
        texture = cls(width, height, srgb=srgb)
        texture.upload(image)
        return texture

    def upload(self, image: np.ndarray) -> None:
        """画素を書き込む。大きさが違えば作り直す。"""
        if image.ndim != 3 or image.shape[2] != 4 or image.dtype != np.uint8:
            raise ValueError(f"RGBA uint8 の配列が必要: shape={image.shape}, dtype={image.dtype}")

        height, width = image.shape[:2]
        data = np.ascontiguousarray(image)
        GL.glBindTexture(GL.GL_TEXTURE_2D, self.handle)
        GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, 1)
        if (width, height) != (self.width, self.height):
            GL.glTexImage2D(
                GL.GL_TEXTURE_2D,
                0,
                self._internal_format,
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


class Framebuffer:
    """描画先。カラーテクスチャ 1 枚を持つ。

    既定は ``RGBA16F``。エフェクトを重ねるとリニア値が 0..1 に収まらないことが
    あり（グローの加算など）、8bit だとそこで潰れて後段のエフェクトに渡らない。
    """

    def __init__(self, width: int, height: int, *, internal_format: int = GL.GL_RGBA16F) -> None:
        self.width = max(1, width)
        self.height = max(1, height)
        self._internal_format = internal_format
        self.color = 0
        self.handle = 0
        self._create()

    def _create(self) -> None:
        self.color = int(GL.glGenTextures(1))
        GL.glBindTexture(GL.GL_TEXTURE_2D, self.color)
        _set_sampling()
        GL.glTexImage2D(
            GL.GL_TEXTURE_2D,
            0,
            self._internal_format,
            self.width,
            self.height,
            0,
            GL.GL_RGBA,
            GL.GL_FLOAT if self._internal_format == GL.GL_RGBA16F else GL.GL_UNSIGNED_BYTE,
            None,
        )

        self.handle = int(GL.glGenFramebuffers(1))
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.handle)
        GL.glFramebufferTexture2D(
            GL.GL_FRAMEBUFFER, GL.GL_COLOR_ATTACHMENT0, GL.GL_TEXTURE_2D, self.color, 0
        )
        status = GL.glCheckFramebufferStatus(GL.GL_FRAMEBUFFER)
        if status != GL.GL_FRAMEBUFFER_COMPLETE:
            raise ShaderError(f"フレームバッファを構成できない: 0x{status:x}")
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, 0)

    def resize(self, width: int, height: int) -> None:
        if (max(1, width), max(1, height)) == (self.width, self.height):
            return
        self.release()
        self.width, self.height = max(1, width), max(1, height)
        self._create()

    def bind(self, *, clear: tuple[float, float, float, float] | None = None) -> None:
        """描画先にする。``clear`` を渡すとその色で塗り潰す。"""
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.handle)
        GL.glViewport(0, 0, self.width, self.height)
        if clear is not None:
            GL.glClearColor(*clear)
            GL.glClear(GL.GL_COLOR_BUFFER_BIT)

    def release(self) -> None:
        if self.handle:
            GL.glDeleteFramebuffers(1, [self.handle])
            self.handle = 0
        if self.color:
            GL.glDeleteTextures([self.color])
            self.color = 0


def _set_sampling() -> None:
    """拡大縮小は線形、端は繰り返さずに引き伸ばす。

    端を繰り返すと、ぼかしの際に反対側の色が回り込む。
    """
    GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_LINEAR)
    GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_LINEAR)
    GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_S, GL.GL_CLAMP_TO_EDGE)
    GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_WRAP_T, GL.GL_CLAMP_TO_EDGE)


def _compile(kind: int, source: str) -> int:
    shader = int(GL.glCreateShader(kind))
    GL.glShaderSource(shader, source)
    GL.glCompileShader(shader)
    if not GL.glGetShaderiv(shader, GL.GL_COMPILE_STATUS):
        log = _decode(GL.glGetShaderInfoLog(shader))
        raise ShaderError(f"シェーダのコンパイルに失敗: {log}\n{_numbered(source)}")
    return shader


def _numbered(source: str) -> str:
    """エラー箇所を探せるよう、行番号を付けて返す。"""
    return "\n".join(f"{index:3d}| {line}" for index, line in enumerate(source.splitlines(), 1))


def _decode(log: bytes | str) -> str:
    return log.decode("utf-8", "replace") if isinstance(log, bytes) else str(log)
