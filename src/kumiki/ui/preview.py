"""プレビュー表示

``QOpenGLWidget`` の中で合成して、そのまま画面へ出す 合成結果を numpy へ読み出して
``QImage`` にしてから描くこともできるが、それだと毎フレーム GPU → CPU → GPU の
往復が入る 1080p で 8MB の転送が 30 回/秒、再生には致命的
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtOpenGLWidgets import QOpenGLWidget

from kumiki.core.model import Project
from kumiki.engine.cache.proxy import ProxyStore
from kumiki.engine.gpu import CurrentGLContext
from kumiki.engine.render import FULL_QUALITY, FrameRenderer, RenderQuality

__all__ = ["PreviewWidget"]


class PreviewWidget(QOpenGLWidget):
    """タイムラインの指定フレームを映す"""

    #: GL の準備ができた レンダラを使い始めてよい合図
    ready = Signal()

    def __init__(
        self, project: Project, parent: object = None, *, proxies: ProxyStore | None = None
    ) -> None:
        super().__init__(parent)  # type: ignore[arg-type]
        self._project = project
        self._frame = 0
        self._quality = FULL_QUALITY
        self._renderer: FrameRenderer | None = None
        #: プレビュー用の控えの置き場 **書き出しには渡さない**
        self._proxies = proxies
        self.setMinimumSize(240, 135)

    @property
    def renderer(self) -> FrameRenderer | None:
        return self._renderer

    def set_project(self, project: Project) -> None:
        self._project = project
        if self._renderer is not None:
            # レンダラは GL を触るので、コンテキストを current にしてから渡す
            self.makeCurrent()
            self._renderer.set_project(project)
            self.doneCurrent()
        self.update()

    def set_frame(self, frame: int) -> None:
        frame = max(0, frame)
        if frame == self._frame:
            return
        self._frame = frame
        self.update()

    def set_quality(self, quality: RenderQuality) -> None:
        self._quality = quality
        if self._renderer is not None:
            self.makeCurrent()
            self._renderer.set_quality(quality)
            self.doneCurrent()
        self.update()

    def shutdown(self) -> None:
        """GL 資源を解放する ウィンドウを閉じる前に呼ぶこと

        ``QOpenGLWidget`` が破棄された後ではコンテキストが無く、テクスチャの
        解放ができない
        """
        if self._renderer is None:
            return
        self.makeCurrent()
        self._renderer.close()
        self._renderer = None
        self.doneCurrent()

    def initializeGL(self) -> None:  # noqa: N802 - Qt の命名規約
        # ここでは Qt がすでにコンテキストを current にしている 自前の
        # オフスクリーンコンテキストを使うと描画先を見失う
        self._renderer = FrameRenderer(
            self._project,
            context=CurrentGLContext(),
            quality=self._quality,
            proxies=self._proxies,
        )
        self.ready.emit()

    def paintGL(self) -> None:  # noqa: N802 - Qt の命名規約
        if self._renderer is None:
            return
        ratio = self.devicePixelRatioF()
        width = max(1, int(self.width() * ratio))
        height = max(1, int(self.height() * ratio))

        self._renderer.compose(self._frame)
        self._renderer.compositor.present(self.defaultFramebufferObject(), (0, 0, width, height))
