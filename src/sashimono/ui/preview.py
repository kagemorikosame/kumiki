"""プレビュー表示

``QOpenGLWidget`` の中で合成して、そのまま画面へ出す 合成結果を numpy へ読み出して
``QImage`` にしてから描くこともできるが、それだと毎フレーム GPU → CPU → GPU の
往復が入る 1080p で 8MB の転送が 30 回/秒、再生には致命的
"""

from __future__ import annotations

import time
from collections.abc import Collection

from PySide6.QtCore import QTimer, Signal
from PySide6.QtGui import QOpenGLContext
from PySide6.QtOpenGLWidgets import QOpenGLWidget

from sashimono.core.model import MediaId, Project
from sashimono.engine.cache.proxy import ProxyStore
from sashimono.engine.gpu import CurrentGLContext
from sashimono.engine.render import (
    FULL_QUALITY,
    FrameRenderer,
    Invalidation,
    PreviewCache,
    RenderQuality,
    changed_spans,
    image_spans,
)

__all__ = ["SLOW_FRAME_MS", "PreviewWidget"]

#: 先読みの 1 コマにこれ以上掛かるなら、先読みそのものをやめる（ミリ秒）
#:
#: 描いているのは編集画面と同じ GL コンテキストなので、1 コマ描く間は
#: 操作を受け付けられない 重すぎる素材（動画を 20 本重ねて効果を積むと
#: 1 コマ 2 秒 測った値は sashimono.engine.cache.proxy）では、貯まる値打ちより
#: 固まる方が大きい 0.2 秒は、押してから反応するまでに引っかかりを感じ始める辺り
SLOW_FRAME_MS = 200.0

#: エフェクトが読む画像（画像合成の絵、縁取りの模様）が書き換わったかを見る間隔（ミリ秒）
#:
#: 画像はパスで指すだけなので、別のソフトで描き直してもプロジェクトは変わらず、
#: 先読みした絵が古いまま残る 見るのは更新時刻・大きさ・ファイルの番号だけで
#: 中身は読まない（1 枚あたり stat 1 回、数十マイクロ秒）ので、いま使っている
#: 画像を 1 秒おきに見ても手間にならない 描き直してから画面へ
#: 戻ってくるまでの間には気付ける
IMAGE_WATCH_MS = 1000


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
        #: まだ置き場へ渡していないメモリの量 GL を確実に使える所で渡す
        self._pending_budget: int | None = None
        #: 手が空いたら 1 コマずつ描く 間隔 0 は「ほかにすることが無くなったら」
        #: という意味で、入力の処理より後になる 待ち時間を入れると、貯まるまでが
        #: 枚数 × その待ち時間ぶん延びる
        self._idle = QTimer(self)
        self._idle.setInterval(0)
        self._idle.timeout.connect(self._prefetch_step)
        #: 再生中は先読みを止める 出す側と同じ GPU を奪い合って、
        #: いま出すべきコマが遅れる
        self._playing = False
        self._image_watch = QTimer(self)
        self._image_watch.setInterval(IMAGE_WATCH_MS)
        self._image_watch.timeout.connect(self.check_images)
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

    def refresh_all(self) -> None:
        """取ってある絵を全部捨てて描き直す 絵の出方を変える設定を切り替えたとき

        中身（プロジェクト）は同じなので、編集の差分からは捨てる範囲が出てこない
        """
        if self._renderer is not None:
            self.makeCurrent()
            self._invalidate(Invalidation.all())
            self.doneCurrent()
        self.update()
        self._restart_prefetch()

    def check_images(self) -> None:
        """エフェクトが読む画像が書き換わっていたら、それを使う所を描き直す

        **コンテキストを current にしない** タイマーから呼ばれるので、窓が隠れて
        いると current にならないまま戻り、そこで doneCurrent を呼ぶとほかが使って
        いるコンテキストを外してしまう ここで捨てる絵は描画先を空きへ回すだけで
        （:meth:`FrameCache.invalidate` は GL を呼ばない）、current でなくてよい
        current にできなかったからと捨てずに戻ると、書き換わりは 1 度しか
        伝わらないので、古い絵が残ったままになる
        """
        if self._renderer is None or self._cache is None:
            return
        changed = self._renderer.stale_images()
        if not changed:
            return
        self._cache.invalidate(image_spans(self._project, changed))
        self.update()
        self._restart_prefetch()

    def set_prefetch_bytes(self, prefetch_bytes: int) -> None:
        """先読みに使えるメモリを変える 0 で止める

        その場では渡さない 減らすと置き場は描画先を手放すので GL を触るが、
        設定の窓から戻ってきた所が GL を使える状態とは限らない
        描く直前（:meth:`paintGL` と先読みの 1 コマ）まで持ち越す
        """
        prefetch_bytes = max(0, prefetch_bytes)
        if prefetch_bytes == self._prefetch_bytes:
            return
        self._prefetch_bytes = prefetch_bytes
        self._pending_budget = prefetch_bytes
        self.update()
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
        self._image_watch.stop()
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
        self._image_watch.start()
        self.ready.emit()
        self._restart_prefetch()

    def paintGL(self) -> None:  # noqa: N802 - Qt の命名規約
        if self._renderer is None:
            return
        ratio = self.devicePixelRatioF()
        width = max(1, int(self.width() * ratio))
        height = max(1, int(self.height() * ratio))

        # Qt はここでコンテキストを current にしている 持ち越した設定を当てる
        self._apply_pending()
        if self._cache is not None and self._cache.enabled:
            self._cache.draw(self._frame, self.defaultFramebufferObject(), (0, 0, width, height))
            return
        self._renderer.compose(self._frame)
        self._renderer.compositor.present(self.defaultFramebufferObject(), (0, 0, width, height))

    def _apply_pending(self) -> None:
        """持ち越していた設定を置き場へ渡す **コンテキストが current な所で呼ぶこと**"""
        if self._pending_budget is None or self._cache is None:
            return
        budget, self._pending_budget = self._pending_budget, None
        self._cache.set_budget(budget, self._frame)
        # 0 から増やしたときは、ここで初めて先読みできるようになる
        self._restart_prefetch()

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
        if self._playing or self._cache is None:
            # タイマーを止めても、すでに積まれた合図は届く 再生が始まった後に
            # 1 コマ描くと、出す側と GL を奪い合う
            self._idle.stop()
            return
        started = time.perf_counter()
        try:
            # コンテキストを current にする所も中へ入れる ここで落ちたときに
            # だけ外へ抜けるのでは、守ったことにならない
            self.makeCurrent()
            if QOpenGLContext.currentContext() is not self.context():
                # **戻り値では分からない** makeCurrent は何も返さず、窓が隠れている
                # ときや端末が休んだ後は current にならないまま戻る
                # そのまま GL を触ると、別のコンテキストへ描くことになる
                self._idle.stop()
                self.prefetch_stopped.emit("GL を使えないので先読みを止めた")
                return
            try:
                self._apply_pending()
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
        if elapsed >= SLOW_FRAME_MS:
            # 1 コマにこれだけ掛かるなら、貯まるまでずっと操作を受け付けられない
            # 次に再生ヘッドか中身が変わったら、また試す
            self._idle.stop()
            self.prefetch_stopped.emit(f"1 コマ {elapsed / 1000:.1f} 秒掛かるので先読みを止めた")
