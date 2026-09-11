"""GPU での映像合成。

色は必ずリニア空間で扱う。素材は sRGB で符号化されているので、テクスチャを
``GL_SRGB8_ALPHA8`` で作ってサンプリング時に自動でリニアへ戻し、合成を
``RGBA16F`` のフレームバッファ上で行い、最後に sRGB へ符号化して出す。

符号化されたままの値を足し引きすると、半透明の重ねやフェードが暗く沈む。
これは「なんとなく違う」ではなく明確に間違いなので、最初からリニアで通す。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from OpenGL import GL

from kumiki.engine.gpu.glutil import (
    FULL_RECT,
    IDENTITY,
    VERTEX_SHADER,
    Framebuffer,
    Program,
    ScreenQuad,
    Texture,
)

__all__ = [
    "BlendMode",
    "Compositor",
    "Placement",
    "Texture",
    "Transform",
    "fit_placement",
]


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


class BlendMode:
    """トラックを重ねるときの合成方法。

    値は :class:`~kumiki.core.model.Clip` に文字列で入るので、そのまま
    プロジェクトファイルにも出る。名前を変えると古いファイルが読めなくなる。
    """

    NORMAL = "normal"
    ADD = "add"
    MULTIPLY = "multiply"
    SCREEN = "screen"

    ALL = (NORMAL, ADD, MULTIPLY, SCREEN)


#: 合成方法ごとの ``glBlendFuncSeparate`` の設定。
#: どれもストレートアルファ（非乗算済み）前提。
_BLEND_FUNCS: dict[str, tuple[int, int]] = {
    BlendMode.NORMAL: (GL.GL_SRC_ALPHA, GL.GL_ONE_MINUS_SRC_ALPHA),
    BlendMode.ADD: (GL.GL_SRC_ALPHA, GL.GL_ONE),
    BlendMode.MULTIPLY: (GL.GL_DST_COLOR, GL.GL_ONE_MINUS_SRC_ALPHA),
    BlendMode.SCREEN: (GL.GL_ONE_MINUS_DST_COLOR, GL.GL_ONE),
}


@dataclass(frozen=True, slots=True)
class Placement:
    """描画先の矩形。ピクセル単位で、原点は左上。"""

    left: float
    top: float
    width: float
    height: float

    def to_clip(self, target_width: int, target_height: int) -> tuple[float, ...]:
        """クリップ空間 (-1..1) の ``(左, 下, 右, 上)`` へ。"""
        left = self.left / target_width * 2.0 - 1.0
        right = (self.left + self.width) / target_width * 2.0 - 1.0
        # GL の Y は上が正。画像座標とは向きが逆なので入れ替える。
        top = 1.0 - self.top / target_height * 2.0
        bottom = 1.0 - (self.top + self.height) / target_height * 2.0
        return (left, bottom, right, top)


@dataclass(frozen=True, slots=True)
class Transform:
    """回転や拡大を伴う配置。

    値の意味は AviUtl の描画パラメータに合わせてある。移植した資産が同じ数値で
    同じ見た目になるようにするためで、こちら独自の単位に直すと、スクリプトの
    値をいちいち換算することになる。

    - 位置は画面中央からのずれ（ピクセル、Y は下が正）
    - ``zoom`` は 1.0 が等倍
    - ``rotation`` は度。画面上で時計回りが正
    - ``aspect`` は -1..1。正で横が縮み、負で縦が縮む
    - ``pivot`` は回転と拡大の中心を、オブジェクトの中心からずらす量
    """

    x: float = 0.0
    y: float = 0.0
    zoom: float = 1.0
    rotation: float = 0.0
    aspect: float = 0.0
    pivot_x: float = 0.0
    pivot_y: float = 0.0
    #: 軸ごとの追加の倍率。``zoom`` とは別に掛かる。
    scale_x: float = 1.0
    scale_y: float = 1.0

    def scale(self) -> tuple[float, float]:
        """縦横それぞれの倍率。``aspect`` と軸ごとの倍率を反映する。"""
        wide = 1.0 - max(0.0, min(self.aspect, 1.0))
        tall = 1.0 - max(0.0, min(-self.aspect, 1.0))
        return self.zoom * wide * self.scale_x, self.zoom * tall * self.scale_y

    def placement(
        self, source_width: int, source_height: int, target_width: int, target_height: int
    ) -> Placement:
        """回転を除いた配置。中心が画面中央 + ``(x, y)`` に来る。"""
        scale_x, scale_y = self.scale()
        width = max(1.0, source_width * scale_x)
        height = max(1.0, source_height * scale_y)
        return Placement(
            left=target_width / 2.0 + self.x - width / 2.0,
            top=target_height / 2.0 + self.y - height / 2.0,
            width=width,
            height=height,
        )

    def matrix(self, target_width: int, target_height: int) -> tuple[float, ...]:
        """回転をクリップ空間の 3x3 行列にする。

        クリップ空間は縦横で 1 単位あたりのピクセル数が違うので、そのまま回すと
        画面の縦横比の分だけ歪む。ピクセルの尺度へ直してから回し、戻す。
        """
        if not self.rotation:
            return IDENTITY

        radians = math.radians(self.rotation)
        cosine, sine = math.cos(radians), math.sin(radians)
        half_width = target_width / 2.0
        half_height = target_height / 2.0

        # 回転の中心。クリップ空間で。画面の Y は下が正、クリップは上が正。
        pivot_x = (self.x + self.pivot_x) / half_width
        pivot_y = -(self.y + self.pivot_y) / half_height

        # ピクセル尺度へ直して回し、戻す。時計回りを正にするため符号を入れ替える。
        a = cosine
        b = sine * half_height / half_width
        c = -sine * half_width / half_height
        d = cosine
        return (
            a,
            b,
            pivot_x - a * pivot_x - b * pivot_y,
            c,
            d,
            pivot_y - c * pivot_x - d * pivot_y,
            0.0,
            0.0,
            1.0,
        )


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


class Compositor:
    """トラックを重ねて 1 枚の絵にする。

    GL コンテキストが current な状態で使うこと。生成も描画も解放もすべて同じ
    コンテキスト上で行う必要がある。
    """

    def __init__(self, width: int, height: int) -> None:
        if width <= 0 or height <= 0:
            raise ValueError(f"解像度が不正: {width}x{height}")

        self._program = Program(VERTEX_SHADER, _FRAGMENT_SHADER)
        self._resolve_program = Program(VERTEX_SHADER, _RESOLVE_FRAGMENT_SHADER)
        self._quad = ScreenQuad()
        self._canvas = Framebuffer(width, height)
        # 読み出し用。リニアの合成結果を sRGB へ符号化して受け取る。
        self._resolved = Framebuffer(width, height, internal_format=GL.GL_RGBA8)

    @property
    def width(self) -> int:
        return self._canvas.width

    @property
    def height(self) -> int:
        return self._canvas.height

    @property
    def quad(self) -> ScreenQuad:
        """全画面四角形。エフェクト処理と共有する。"""
        return self._quad

    def resize(self, width: int, height: int) -> None:
        if width <= 0 or height <= 0:
            raise ValueError(f"解像度が不正: {width}x{height}")
        self._canvas.resize(width, height)
        self._resolved.resize(width, height)

    def begin(self, background: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 1.0)) -> None:
        """合成を始める。背景色はリニア値で指定する。"""
        self._canvas.bind(clear=background)
        GL.glEnable(GL.GL_BLEND)
        self._set_blend(BlendMode.NORMAL)

    def draw(
        self,
        texture: Texture,
        *,
        placement: Placement | None = None,
        opacity: float = 1.0,
        flip: bool = True,
        blend: str = BlendMode.NORMAL,
        matrix: tuple[float, ...] | None = None,
    ) -> None:
        """1 枚を重ねる。

        ``flip`` の既定が真なのは、デコードした画像が左上原点で、GL のテクスチャ座標が
        左下原点だから。素材由来の画像はほぼ常に反転が要る。エフェクトを通した後の
        テクスチャはすでに GL の向きなので、そちらは偽で渡す。
        """
        if placement is None:
            placement = fit_placement(texture.width, texture.height, self.width, self.height)
        self.draw_handle(
            texture.handle, placement, opacity=opacity, flip=flip, blend=blend, matrix=matrix
        )

    def draw_handle(
        self,
        handle: int,
        placement: Placement,
        *,
        opacity: float = 1.0,
        flip: bool = True,
        blend: str = BlendMode.NORMAL,
        matrix: tuple[float, ...] | None = None,
    ) -> None:
        """GL のテクスチャ番号を直接指定して重ねる。

        エフェクトを通した結果はフレームバッファの中にあり、:class:`Texture` では
        包まれていない。それを合成するための入口。

        ``matrix`` を渡すと、矩形に加えてその変換が掛かる（回転など）。
        """
        self._canvas.bind()
        GL.glEnable(GL.GL_BLEND)
        self._set_blend(blend)

        self._program.use()
        self._program.set_vec4("u_rect", placement.to_clip(self.width, self.height))
        self._program.set_bool("u_flip", flip)
        self._program.set_float("u_opacity", float(np.clip(opacity, 0.0, 1.0)))
        self._program.set_mat3("u_transform", matrix if matrix is not None else IDENTITY)
        self._program.bind_texture("u_texture", handle)
        self._quad.draw()
        if matrix is not None:
            # 次の描画へ持ち越さない。持ち越すと、回転を掛けた次のクリップまで
            # 一緒に回る。
            self._program.set_mat3("u_transform", IDENTITY)

    def read(self) -> np.ndarray:
        """合成結果を sRGB 符号化した ``(高さ, 幅, 4)`` の uint8 配列で返す。"""
        # リニアの結果をもう 1 パス通して sRGB へ符号化する。glReadPixels に
        # RGBA16F から直接 uint8 で読ませると、変換式が実装依存になる。
        self._resolve(self._resolved.handle, (0, 0, self.width, self.height))

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

    def release(self) -> None:
        self._canvas.release()
        self._resolved.release()
        self._quad.release()
        self._program.release()
        self._resolve_program.release()

    def _set_blend(self, mode: str) -> None:
        source, destination = _BLEND_FUNCS.get(mode, _BLEND_FUNCS[BlendMode.NORMAL])
        GL.glBlendFuncSeparate(source, destination, GL.GL_ONE, GL.GL_ONE_MINUS_SRC_ALPHA)

    def _resolve(self, framebuffer: int, viewport: tuple[int, int, int, int]) -> None:
        """リニアの合成結果を sRGB へ符号化して ``framebuffer`` へ描く。"""
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, framebuffer)
        GL.glViewport(*viewport)
        GL.glDisable(GL.GL_BLEND)
        self._resolve_program.use()
        self._resolve_program.set_vec4("u_rect", FULL_RECT)
        self._resolve_program.set_bool("u_flip", False)
        self._resolve_program.bind_texture("u_texture", self._canvas.color)
        self._quad.draw()
