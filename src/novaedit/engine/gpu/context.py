"""OpenGL コンテキストの管理。

コンテキストは Qt に持たせる。``QOpenGLWidget`` と共有できるので、プレビュー用と
書き出し用でテクスチャやシェーダを作り直さずに済む。GLFW などを別に持ち込むと、
Qt のコンテキストと二重管理になり、共有もできなくなる。

ヘッドレス（GUI を出さない書き出しやテスト）でも同じ経路を通る。``QOffscreenSurface``
に描くだけで、実機の GPU がそのまま使える。
"""

from __future__ import annotations

from types import TracebackType

from PySide6.QtCore import QCoreApplication
from PySide6.QtGui import (
    QGuiApplication,
    QOffscreenSurface,
    QOpenGLContext,
    QSurface,
    QSurfaceFormat,
)

__all__ = [
    "GLContextError",
    "OffscreenGLContext",
    "ensure_qt_application",
    "preferred_surface_format",
]

#: 要求する OpenGL のバージョン。フレームバッファの浮動小数点フォーマットと
#: コンピュートシェーダ（将来のエフェクト用）が使える最低ラインとして 4.3 を選ぶ。
REQUIRED_GL_VERSION = (4, 3)


class GLContextError(RuntimeError):
    """OpenGL コンテキストを用意できない。"""


def preferred_surface_format() -> QSurfaceFormat:
    """アプリ全体で使うサーフェス形式。

    ``QApplication`` を作る前に :meth:`QSurfaceFormat.setDefaultFormat` へ渡すこと。
    後から設定してもウィジェットのコンテキストには反映されない。
    """
    fmt = QSurfaceFormat()
    fmt.setVersion(*REQUIRED_GL_VERSION)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    fmt.setDepthBufferSize(0)
    fmt.setStencilBufferSize(0)
    fmt.setSwapBehavior(QSurfaceFormat.SwapBehavior.DoubleBuffer)
    return fmt


def ensure_qt_application() -> QCoreApplication:
    """Qt のアプリケーションオブジェクトを用意する。

    GL コンテキストは Qt のイベントループが無くても作れるが、``QGuiApplication``
    の存在は要る。書き出しやテストから呼ばれたときのために、無ければここで作る。
    """
    existing = QCoreApplication.instance()
    if existing is not None:
        return existing
    QSurfaceFormat.setDefaultFormat(preferred_surface_format())
    return QGuiApplication([])


class OffscreenGLContext:
    """画面を持たない GL コンテキスト。

    ``share`` に既存のコンテキストを渡すと、テクスチャやバッファを共有できる。
    プレビューウィジェットが作ったテクスチャを書き出し側から読む、といった用途向け。
    """

    def __init__(self, share: QOpenGLContext | None = None) -> None:
        ensure_qt_application()
        fmt = preferred_surface_format()

        self._surface = QOffscreenSurface()
        self._surface.setFormat(fmt)
        self._surface.create()
        if not self._surface.isValid():
            raise GLContextError("オフスクリーンサーフェスを作れない")

        self._context = QOpenGLContext()
        self._context.setFormat(fmt)
        if share is not None:
            self._context.setShareContext(share)
        if not self._context.create():
            raise GLContextError(
                f"OpenGL {REQUIRED_GL_VERSION[0]}.{REQUIRED_GL_VERSION[1]} "
                "のコンテキストを作れない。GPU ドライバを確認すること"
            )

        self._depth = 0

    @property
    def context(self) -> QOpenGLContext:
        return self._context

    @property
    def surface(self) -> QSurface:
        return self._surface

    def make_current(self) -> None:
        if not self._context.makeCurrent(self._surface):
            raise GLContextError("GL コンテキストを current にできない")

    def done_current(self) -> None:
        self._context.doneCurrent()

    def __enter__(self) -> OffscreenGLContext:
        # 入れ子にできるようにしておく。合成の途中でテクスチャを作るような経路で、
        # 内側が抜けた拍子にコンテキストが外れると診断の難しい失敗になる。
        if self._depth == 0:
            self.make_current()
        self._depth += 1
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._depth -= 1
        if self._depth == 0:
            self.done_current()

    def release(self) -> None:
        """コンテキストとサーフェスを破棄する。"""
        if self._depth:
            self.done_current()
            self._depth = 0
        self._surface.destroy()
