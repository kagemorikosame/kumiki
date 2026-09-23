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
    DEFAULT_DECODE_THREADS,
    FULL_QUALITY,
    FrameRenderer,
    Invalidation,
    PreviewCache,
    RenderQuality,
    changed_spans,
    image_spans,
)
from sashimono.engine.render.background import BackgroundPrefetch

__all__ = ["SLOW_FRAME_MS", "PreviewWidget"]

#: 先読みの 1 コマにこれ以上掛かるなら、先読みそのものをやめる（ミリ秒）
#: **画面のスレッドで貯めるときだけ** 別のスレッドで貯めるときは画面が止まらないので、
#: 重い所ほど貯める値打ちがある
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
        decode_threads: int = DEFAULT_DECODE_THREADS,
        prefetch_thread: bool = True,
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
        #: レイヤーごとの並列デコードのスレッド数 GL を作る前に決まっていることがある
        self._decode_threads = decode_threads
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
        #: 先読みを別のスレッドで描くか（設定） 切ると画面のスレッドで 1 コマずつ描く
        self._prefetch_thread = prefetch_thread
        #: 別のスレッドの先読み 作れなかった・止まったときは None のまま、
        #: 画面のスレッドの先読み（``_idle`` と ``_cache``）へ戻る
        self._background: BackgroundPrefetch | None = None
        #: 閉じた走り係が捨てていた控えの素材 次の :meth:`take_discarded` で渡す
        self._closed_discarded: set[MediaId] = set()
        #: 別のスレッドで先読みできなかった 何度も作り直して失敗し続けないよう覚える
        #: 設定を入れ直したときだけ忘れる
        self._background_broken = False
        #: 再生を始めたコマを描き終えたら、貯めた所の端を裏で読ませる（:meth:`_prime_edge`）
        self._prime_after_paint = False
        self.setMinimumSize(240, 135)

    @property
    def renderer(self) -> FrameRenderer | None:
        return self._renderer

    def set_project(self, project: Project) -> None:
        previous = self._project
        self._project = project
        if self._renderer is not None:
            changed = changed_spans(previous, project)
            # レンダラは GL を触るので、コンテキストを current にしてから渡す
            self.makeCurrent()
            self._renderer.set_project(project)
            self._invalidate(changed)
            self.doneCurrent()
            if self._background is not None:
                # 捨てる範囲とプロジェクトを 1 つの頼みで渡す 別々に渡すと、
                # 走り係が新しいプロジェクトで描く前に古い絵を出せる間ができる
                self._background.set_project(project, changed)
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
            if self._background is not None:
                self._background.set_proxies(proxies)
        self.update()
        self._restart_prefetch()

    def take_discarded(self) -> set[MediaId]:
        """レンダラが捨てた控えの素材 呼ぶ側が作り直しを頼む

        先読みの走り係のレンダラが捨てた分も合わせる 走り係は画面の側より先の
        コマを読むので、壊れた控えに先に当たるのはたいてい走り係の方
        """
        found = self._renderer.take_discarded() if self._renderer is not None else set()
        found |= self._closed_discarded
        self._closed_discarded = set()
        if self._background is not None:
            found |= self._background.take_discarded()
        return found

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
        if self._background is not None:
            self._background.reopen_sources(media_ids)
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
        if self._background is not None:
            self._background.set_quality(quality)
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
        if self._background is not None:
            self._background.invalidate(Invalidation.all())
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
        spans = image_spans(self._project, changed)
        self._cache.invalidate(spans)
        if self._background is not None:
            # 走り係への頼みも GL を触らない 一覧から外すのはこちらのスレッドで済む
            self._background.invalidate(spans)
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
        if prefetch_bytes == 0:
            # 切ったら走り係ごと止める 予算 0 を渡すだけでは、スレッド・レンダラ・
            # デコーダ（素材のファイルを掴んだまま）・共有したコンテキスト・効果の
            # GPU の資源が窓を閉じるまで残る 入れ直せば次の空き時間に作り直す
            self._close_background()
        elif self._background is not None:
            # 走り係は自分のスレッドで描画先を手放すので、その場で渡してよい
            self._background.set_budget(prefetch_bytes, self._frame)
        self.update()
        self._restart_prefetch()

    def set_decode_threads(self, threads: int) -> None:
        """レイヤーごとの並列デコードのスレッド数を変える 1 で並べない

        GL を触らないので、その場でレンダラへ渡してよい まだ GL ができていない
        （``initializeGL`` の前）なら、覚えておいて作るときに渡す
        """
        if threads == self._decode_threads:
            return
        self._decode_threads = threads
        if self._renderer is not None:
            self._renderer.set_decode_threads(threads)
        if self._background is not None:
            self._background.set_decode_threads(threads)

    def set_prefetch_thread(self, enabled: bool) -> None:
        """先読みを別のスレッドで描くかを切り替える

        切ったら走り係を止めて、画面のスレッドで貯め直す 入れ直したときは、
        前に作れなかったことも忘れる（ドライバを入れ替えた後などに試せるように）
        """
        if enabled == self._prefetch_thread:
            return
        self._prefetch_thread = enabled
        self._background_broken = False
        if not enabled:
            self._close_background()
        self._restart_prefetch()

    @property
    def prefetch_in_background(self) -> bool:
        """いま別のスレッドで先読みしているか"""
        return self._background is not None

    def set_playing(self, playing: bool) -> None:
        """再生中かどうか 再生中は先読みを止める"""
        self._playing = playing
        self._prime_after_paint = False
        if playing:
            if self._background is not None and self._frame not in self._background.cached:
                # いまのコマは画面の側でこれから描く 先に端を頼むと、描くときに
                # 同じデコーダの頼みを待ったうえで、頭へ戻って読み直す
                # 描き終えてから頼む
                self._prime_after_paint = True
            else:
                self._prime_edge()
        self._restart_prefetch()

    def _prime_edge(self) -> None:
        """再生を始めたら、貯めた所の端のコマのデコードを裏で走らせておく

        別のスレッドで貯めると、画面の側のデコーダは貯めた所を再生する間は動かない
        端を越えた 1 コマ目で鍵フレームから読み直し、再生がそこで 1 度引っかかる
        （GOP 250 の 1080p を 3 枚重ねて 129ms 画面のスレッドで貯めたときは 14ms、
        ここで頼んでおくと 12ms）
        画面のスレッドで貯めるときは、貯めたデコーダがそのまま端に居るので要らない
        """
        if self._background is None or self._renderer is None:
            return
        cached = self._background.cached
        # いまのコマは貯まっていないことがある 画面の側で描いたコマは、走り係に
        # 描かせない（次のコマから貯めさせる）ので、その次から数える
        start = self._frame if self._frame in cached else self._frame + 1
        edge = start
        while edge in cached:
            edge += 1
        if edge == start:
            # 先に貯まった所が無い 再生すれば描く道がそのまま順に読む
            return
        if edge < self._project.duration:
            self._renderer.prime(edge)

    @property
    def cached_frames(self) -> frozenset[int]:
        """取ってある絵のフレーム番号 どこまで貯まったかを画面に出すため"""
        if self._background is not None:
            return self._background.cached
        return self._cache.cache.cached if self._cache is not None else frozenset()

    def shutdown(self) -> None:
        """GL 資源を解放する ウィンドウを閉じる前に呼ぶこと

        ``QOpenGLWidget`` が破棄された後ではコンテキストが無く、テクスチャの
        解放ができない
        """
        self._idle.stop()
        self._image_watch.stop()
        # 走り係を先に止める 走り係のコンテキストは画面のコンテキストと共有しているので、
        # 画面の側を先に捨てると、走り係が使っている共有の資源まで消えることがある
        self._close_background()
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
        # 窓を付け替えるとコンテキストが作り直され、ここへもう 1 度来る 前の走り係は
        # 前のコンテキストと共有していて、新しいコンテキストからは絵が見えない
        # 止めておけば、次の空き時間に新しいコンテキストと共有して作り直す
        self._close_background()
        # ここでは Qt がすでにコンテキストを current にしている 自前の
        # オフスクリーンコンテキストを使うと描画先を見失う
        self._renderer = FrameRenderer(
            self._project,
            context=CurrentGLContext(),
            quality=self._quality,
            proxies=self._proxies,
            decode_threads=self._decode_threads,
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
        target = self.defaultFramebufferObject()
        if self._background is not None:
            # 走り係の絵を出すだけ 外れたらこちらで描くが、取ってはおかない
            # 取っておくと画面の側にも描画先を持つことになり、同じメモリで
            # 貯まる枚数が減る 再生ヘッドの所は走り係が真っ先に描く
            if self._background.show(
                self._frame, self._renderer.compositor, target, (0, 0, width, height)
            ):
                return
            # このコマはこちらで描く 走り係には次のコマから貯めさせる 同じコマを
            # 走り係も描くと、GIL と GPU を取り合って両方が遅くなり、再生ヘッドを
            # 送り続ける間は走り係がいつまでも追い付けない
            self._background.set_playhead(self._frame + 1)
            self._renderer.compose(self._frame)
            self._renderer.compositor.present(target, (0, 0, width, height))
            if self._prime_after_paint:
                self._prime_after_paint = False
                self._prime_edge()
            return
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
        if self._background is not None:
            # 走り係は自分で回る 再生ヘッドと、再生中かどうかを渡すだけ
            self._idle.stop()
            self._background.set_paused(self._playing)
            self._background.set_playhead(self._frame)
            return
        if self._playing:
            self._idle.stop()
            return
        if self._wants_background():
            # 走り係はイベントループへ戻ってから作る ここは initializeGL や paintGL の
            # 中からも呼ばれ、そこで別のコンテキストを作ると画面のコンテキストが外れる
            self._idle.start()
            return
        if self._cache is None or not self._cache.enabled:
            self._idle.stop()
            return
        self._idle.start()

    def _wants_background(self) -> bool:
        """別のスレッドの先読みを作りに行くか"""
        if not self._prefetch_thread or self._background_broken or self._prefetch_bytes <= 0:
            return False
        if self._renderer is None or self._cache is None:
            return False
        context = self.context()
        return context is not None and context.isValid()

    def _start_background(self) -> bool:
        """別のスレッドの先読みを作る 作れたら ``True``

        作れなければ、理由を伝えて画面のスレッドの先読みへ戻る 共有した
        コンテキストを作れないドライバや、GL の版が足りない仮想環境がある
        """
        assert self._renderer is not None and self._cache is not None
        try:
            # 画面のスレッドで貯めていた絵を先に手放す 残すと、同じメモリを
            # 2 か所で使うことになる current にできなければ、手放すのは後回し
            self.makeCurrent()
            if QOpenGLContext.currentContext() is self.context():
                try:
                    self._cache.release()
                finally:
                    self.doneCurrent()
            background = BackgroundPrefetch(
                self._project,
                share=self.context(),
                quality=self._quality,
                proxies=self._proxies,
                decode_threads=self._decode_threads,
                budget_bytes=self._prefetch_bytes,
                playhead=self._frame,
            )
        except Exception as exc:
            # 共有したコンテキストを作れないドライバや、GL の版が足りない環境がある
            # 先読みそのものは画面のスレッドで続けられる
            self._background_broken = True
            self.prefetch_stopped.emit(
                f"別のスレッドで先読みできないので、画面のスレッドで先読みする: {exc}"
            )
            return False
        background.failed.connect(self._background_failed)
        background.ended.connect(self._background_ended)
        self._background = background
        self._restart_prefetch()
        return True

    def _background_failed(self, background: object, reason: str) -> None:
        """走り係が 1 コマ描けなかった 走り係は次に何か変わるまで休む"""
        if background is self._background:
            self.prefetch_stopped.emit(reason)

    def _background_ended(self, background: object, reason: str) -> None:
        """走り係が続けられなくなった 画面のスレッドの先読みへ戻る

        閉じた後の走り係から遅れて届いた合図は捨てる 拾うと、設定で切って
        入れ直しただけの走り係まで「作れない」扱いになる
        """
        if background is not self._background:
            return
        self._background_broken = True
        self._close_background()
        self.prefetch_stopped.emit(reason)
        self._restart_prefetch()

    def _close_background(self) -> None:
        background, self._background = self._background, None
        if background is not None:
            background.close()
            # 閉じる前に走り係が捨てた控えも預かる 捨てたまま閉じると、壊れた控えを
            # 作り直す頼みが出ず、その素材は元の素材から読み続ける
            self._closed_discarded |= background.take_discarded()

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
        if self._background is not None:
            self._idle.stop()
            return
        if self._wants_background() and self._start_background():
            return
        if not self._cache.enabled and self._pending_budget is None:
            # 走り係を作れなかった所へ来た 画面のスレッドにも貯める場所が無い
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
