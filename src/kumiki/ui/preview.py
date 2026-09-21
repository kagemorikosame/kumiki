"""プレビュー表示

``QOpenGLWidget`` の中で合成して、そのまま画面へ出す 合成結果を numpy へ読み出して
``QImage`` にしてから描くこともできるが、それだと毎フレーム GPU → CPU → GPU の
往復が入る 1080p で 8MB の転送が 30 回/秒、再生には致命的
"""

from __future__ import annotations

import time
from collections.abc import Collection

from PySide6.QtCore import QTimer, Signal
from PySide6.QtOpenGLWidgets import QOpenGLWidget

from kumiki.core.model import MediaId, Project
from kumiki.engine.cache.proxy import ProxyStore
from kumiki.engine.gpu import CurrentGLContext
from kumiki.engine.render import (
    FULL_QUALITY,
    FrameRenderer,
    Invalidation,
    PreviewCache,
    RenderQuality,
    changed_spans,
)

__all__ = ["SLOW_FRAME_MS", "PreviewWidget"]

#: 先読みの 1 コマにこれ以上掛かるなら、先読みそのものをやめる（ミリ秒）
#:
#: 描いているのは編集画面と同じ GL コンテキストなので、1 コマ描く間は
#: 操作を受け付けられない 重すぎる素材（動画を 20 本重ねて効果を積むと
#: 1 コマ 2 秒 測った値は kumiki.engine.cache.proxy）では、貯まる値打ちより
#: 固まる方が大きい 0.2 秒は、押してから反応するまでに引っかかりを感じ始める辺り
SLOW_FRAME_MS = 200.0


class PreviewWidget(QOpenGLWidget):
    """タイムラインの指定フレームを映す"""

    #: GL の準備ができた レンダラを使い始めてよい合図
    ready = Signal()
    #: 先読みをやめた 引数は理由 画面へ出して、黙って効かない状態を避ける
    prefetch_stopped = Signal(str)

    def __init__(
        self,
        project: Project,
        parent: object = None,
        *,
        proxies: ProxyStore | None = None,
        prefetch_bytes: int = 0,
    ) -> None:
        super().__init__(parent)  # type: ignore[arg-type]
        self._project = project
        self._frame = 0
        self._quality = FULL_QUALITY
        self._renderer: FrameRenderer | None = None
        #: プレビュー用の控えの置き場 **書き出しには渡さない**
        self._proxies = proxies
        #: 先読みに使えるバイト数 0 なら先読みしない
        self._prefetch_bytes = max(0, prefetch_bytes)
        self._cache: PreviewCache | None = None
        #: 手が空いたら 1 コマずつ描く 間隔 0 は「ほかにすることが無くなったら」
        #: という意味で、入力の処理より後になる 待ち時間を入れると、貯まるまでが
        #: 枚数 × その待ち時間ぶん延びる
        self._idle = QTimer(self)
        self._idle.setInterval(0)
        self._idle.timeout.connect(self._prefetch_step)
        #: 再生中は先読みを止める 出す側と同じ GPU を奪い合って、
        #: いま出すべきコマが遅れる
        self._playing = False
        self.setMinimumSize(240, 135)

    @property
    def renderer(self) -> FrameRenderer | None:
        return self._renderer

    def set_project(self, project: Project) -> None:
        previous = self._project
        self._project = project
        if self._renderer is not None:
            # レンダラは GL を触るので、コンテキストを current にしてから渡す
            self.makeCurrent()
            self._renderer.set_project(project)
            self._invalidate(changed_spans(previous, project))
            self.doneCurrent()
        self.update()
        self._restart_prefetch()

    def set_proxies(self, proxies: ProxyStore | None) -> None:
        """控えの置き場を差し替える

        開いているデコーダは元のファイルを掴んだままなので、開き直させる
        （設定で切ったのに控えのままだと、切った意味が無い）
        """
        if proxies is self._proxies:
            return
        self._proxies = proxies
        if self._renderer is not None:
            self.makeCurrent()
            self._renderer.set_proxies(proxies)
            # 読む元が変われば絵も変わる 取ってある絵は全部使えない
            self._invalidate(Invalidation.all())
            self.doneCurrent()
        self.update()
        self._restart_prefetch()

    def take_discarded(self) -> set[MediaId]:
        """レンダラが捨てた控えの素材 呼ぶ側が作り直しを頼む"""
        return self._renderer.take_discarded() if self._renderer is not None else set()

    def reload_sources(self, media_ids: Collection[MediaId] | None = None) -> None:
        """素材を開き直させる 控えができた直後に呼ぶ

        描き直すだけでは切り替わらない 先にプレビューした素材は、
        レンダラが元のファイルを掴んだままになっている

        ``media_ids`` を渡すと、その素材のぶんだけ開き直す
        """
        if self._renderer is None:
            return
        self.makeCurrent()
        self._renderer.reopen_sources(media_ids)
        # 控えができた・壊れていて元へ戻した どちらも開き直した素材の絵が変わる
        self._invalidate(Invalidation.all())
        self.doneCurrent()
        self.update()
        self._restart_prefetch()

    def set_frame(self, frame: int) -> None:
        frame = max(0, frame)
        if frame == self._frame:
            return
        self._frame = frame
        self.update()
        self._restart_prefetch()

    def set_quality(self, quality: RenderQuality) -> None:
        self._quality = quality
        if self._renderer is not None:
            self.makeCurrent()
            self._renderer.set_quality(quality)
            self.doneCurrent()
        self.update()
        self._restart_prefetch()

    def set_prefetch_bytes(self, prefetch_bytes: int) -> None:
        """先読みに使えるメモリを変える 0 で止める"""
        prefetch_bytes = max(0, prefetch_bytes)
        if prefetch_bytes == self._prefetch_bytes:
            return
        self._prefetch_bytes = prefetch_bytes
        if self._cache is not None:
            self.makeCurrent()
            self._cache.set_budget(prefetch_bytes)
            self.doneCurrent()
        self._restart_prefetch()

    def set_playing(self, playing: bool) -> None:
        """再生中かどうか 再生中は先読みを止める"""
        self._playing = playing
        self._restart_prefetch()

    @property
    def cached_frames(self) -> frozenset[int]:
        """取ってある絵のフレーム番号 どこまで貯まったかを画面に出すため"""
        return self._cache.cache.cached if self._cache is not None else frozenset()

    def shutdown(self) -> None:
        """GL 資源を解放する ウィンドウを閉じる前に呼ぶこと

        ``QOpenGLWidget`` が破棄された後ではコンテキストが無く、テクスチャの
        解放ができない
        """
        self._idle.stop()
        if self._renderer is None:
            return
        self.makeCurrent()
        if self._cache is not None:
            self._cache.release()
            self._cache = None
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
        self._cache = PreviewCache(self._renderer, budget_bytes=self._prefetch_bytes)
        self.ready.emit()
        self._restart_prefetch()

    def paintGL(self) -> None:  # noqa: N802 - Qt の命名規約
        if self._renderer is None:
            return
        ratio = self.devicePixelRatioF()
        width = max(1, int(self.width() * ratio))
        height = max(1, int(self.height() * ratio))

        if self._cache is not None and self._cache.enabled:
            self._cache.draw(self._frame, self.defaultFramebufferObject(), (0, 0, width, height))
            return
        self._renderer.compose(self._frame)
        self._renderer.compositor.present(self.defaultFramebufferObject(), (0, 0, width, height))

    def _invalidate(self, invalidation: Invalidation) -> None:
        """変わった範囲の先読みを捨てる コンテキストが current な所で呼ぶこと"""
        if self._cache is not None:
            self._cache.invalidate(invalidation)

    def _restart_prefetch(self) -> None:
        """先読みを動かし直す 何か変わるたびに呼ぶ

        止まったままにしない 再生ヘッドが動けば、次に貯める先も変わっている
        """
        if self._cache is None or not self._cache.enabled or self._playing:
            self._idle.stop()
            return
        self._idle.start()

    def _prefetch_step(self) -> None:
        """空き時間に 1 コマだけ描く

        描けなくなったら止める 止めないと、貯まりきった後も空き時間の
        たびに「次はどれか」を数え続ける
        """
        if self._cache is None:
            self._idle.stop()
            return
        started = time.perf_counter()
        try:
            # コンテキストを current にする所も中へ入れる ここで落ちたときに
            # だけ外へ抜けるのでは、守ったことにならない
            self.makeCurrent()
            try:
                filled = self._cache.step(self._frame)
            finally:
                # current にできたときだけ戻す できていないのに戻すと、
                # ほかが使っているコンテキストを外すことになる
                self.doneCurrent()
        except Exception as exc:
            # **先読みの失敗でプレビューを落とさない** ここは Qt のタイマーから
            # 呼ばれるので、投げるとイベントループの外まで抜けてアプリが終わる
            # 先読みは無くても絵は出る 止めて、理由を伝えるに留める
            self._idle.stop()
            self.prefetch_stopped.emit(f"先読みを止めた: {exc}")
            return

        if not filled:
            self._idle.stop()
            return

        elapsed = (time.perf_counter() - started) * 1000
        if elapsed > SLOW_FRAME_MS:
            # 1 コマにこれだけ掛かるなら、貯まるまでずっと操作を受け付けられない
            # 次に再生ヘッドか中身が変わったら、また試す
            self._idle.stop()
            self.prefetch_stopped.emit(f"1 コマ {elapsed / 1000:.1f} 秒掛かるので先読みを止めた")
