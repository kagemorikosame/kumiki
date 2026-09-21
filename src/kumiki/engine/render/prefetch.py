"""先読み（バックグラウンドレンダリング）

編集していない間に、再生ヘッドの先のコマを描いて GPU の上に取っておく
重い所は 1 コマ 100ms 掛かり（1080p で 20 本 × エフェクト 2）、再生しながら
間に合わせることはできない 手が止まっている間に貯めて、再生では出すだけにする

取っておくのは **sRGB へ符号化した 8bit の絵** 合成用の ``RGBA16F`` のままだと
1 枚あたりの大きさが 2 倍になり、同じメモリで貯まる枚数が半分になる
画面へ出す直前の形なので、出すときは転送するだけで済む

測った値は :mod:`kumiki.engine.cache.proxy` の表を見る
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Protocol

from OpenGL import GL
from OpenGL.error import GLError

from kumiki.engine.gpu import Framebuffer, ShaderError
from kumiki.engine.render.invalidate import Invalidation
from kumiki.engine.render.renderer import FrameRenderer

__all__ = ["BYTES_PER_FRAME_PIXEL", "CacheSurface", "FrameCache", "PreviewCache"]

#: 1 画素あたりのバイト数（RGBA 各 8bit）
BYTES_PER_FRAME_PIXEL = 4

#: 再生ヘッドより後ろの絵を、何倍遠いものとして扱うか
#:
#: 再生は前へ進むので、同じ距離なら後ろの絵から捨てる 0 にして後ろを常に
#: 捨てると、行き来しながら詰める編集で片道ぶんが毎回作り直しになる
BACKWARD_WEIGHT = 2


class CacheSurface(Protocol):
    """絵を 1 枚置いておける描画先 :class:`~kumiki.engine.gpu.Framebuffer` が満たす"""

    width: int
    height: int
    handle: int
    color: int

    def release(self) -> None: ...


def _framebuffer(width: int, height: int) -> CacheSurface:
    return Framebuffer(width, height, internal_format=GL.GL_RGBA8)


class FrameCache:
    """描いた絵をフレーム番号で引けるようにしておく

    使えるメモリは決まっているので、入る枚数も決まる いっぱいになったら
    **再生ヘッドから遠い絵**を捨てる 古い順（LRU）で捨てると、再生ヘッドの
    すぐ先に貯めたばかりの絵を、さらに先を貯めるために捨ててしまう

    GL を直に触らないよう、描画先の作り方は差し替えられる（試験では偽物を渡す）
    """

    def __init__(
        self,
        width: int,
        height: int,
        *,
        budget_bytes: int,
        surface: Callable[[int, int], CacheSurface] | None = None,
    ) -> None:
        self._width = max(1, width)
        self._height = max(1, height)
        self._budget = max(0, budget_bytes)
        self._make = surface if surface is not None else _framebuffer
        self._frames: dict[int, CacheSurface] = {}
        self._free: list[CacheSurface] = []
        self._playhead = 0
        #: 実際に取れた上限 GPU のメモリが足りなくなったらここで止める
        #: 予算だけを信じると、載っているメモリの少ない機械で確保に失敗し、
        #: プレビューそのものが描けなくなる
        self._ceiling: int | None = None

    @property
    def width(self) -> int:
        return self._width

    @property
    def height(self) -> int:
        return self._height

    @property
    def capacity(self) -> int:
        """入る枚数 0 なら先読みしない

        予算から出した枚数と、実際に取れた上限の小さい方
        """
        per_frame = self._width * self._height * BYTES_PER_FRAME_PIXEL
        budgeted = self._budget // per_frame
        if self._ceiling is None:
            return budgeted
        return min(budgeted, self._ceiling)

    @property
    def cached(self) -> frozenset[int]:
        return frozenset(self._frames)

    def set_playhead(self, frame: int) -> None:
        self._playhead = max(0, frame)

    def get(self, frame: int) -> CacheSurface | None:
        return self._frames.get(frame)

    def store(self, frame: int, draw: Callable[[CacheSurface], None]) -> CacheSurface | None:
        """``frame`` の絵を描いて取っておく 置く場所が無ければ ``None``

        ``draw`` が投げたら何も残さない 途中まで描いた絵を「描けた」として
        残すと、その位置だけ壊れた絵が出たまま直らない
        """
        existing = self._frames.get(frame)
        if existing is not None:
            return existing
        surface = self._claim(frame)
        if surface is None:
            return None
        try:
            draw(surface)
        except Exception:
            self._free.append(surface)
            raise
        self._frames[frame] = surface
        return surface

    def invalidate(self, invalidation: Invalidation) -> int:
        """変わった範囲の絵を捨てる 捨てた枚数を返す"""
        if not invalidation:
            return 0
        if invalidation.everything:
            count = len(self._frames)
            self._recycle(list(self._frames))
            return count
        dropped = [frame for frame in self._frames if invalidation.contains(frame)]
        self._recycle(dropped)
        return len(dropped)

    def clear(self) -> None:
        """取ってある絵を捨てる 枠は使い回すので手放さない

        取れなかった上限も忘れる 空にしたということは、次に確保するときには
        メモリが空いているかもしれない（ほかのアプリが掴んでいただけのことがある）
        """
        self._ceiling = None
        self._recycle(list(self._frames))

    def resize(self, width: int, height: int) -> None:
        """描く大きさが変わった 取ってあった絵は使えないので手放す

        大きさの違う描画先は使い回せない 残しておくと、いま何枚持っているかと
        使えるメモリの勘定が合わなくなる
        """
        if (max(1, width), max(1, height)) == (self._width, self._height):
            return
        self._width, self._height = max(1, width), max(1, height)
        self._ceiling = None
        self.release()

    def set_budget(self, budget_bytes: int) -> None:
        """使えるメモリを変える 減らしたぶんは遠い絵から手放す

        取れなかった上限は忘れる 一度足りなかったからといって、次も
        足りないとは限らない（ほかのアプリが掴んでいただけのことがある）
        """
        self._budget = max(0, budget_bytes)
        self._ceiling = None
        while len(self._frames) > self.capacity:
            self._frames.pop(self._worst()).release()
        self._release(self._free)
        self._free = []

    def release(self) -> None:
        """GL 資源を手放す コンテキストが current な所で呼ぶこと"""
        self._ceiling = None
        self._release(self._frames.values())
        self._release(self._free)
        self._frames = {}
        self._free = []

    def _claim(self, frame: int) -> CacheSurface | None:
        """``frame`` を置く描画先 遠い絵を追い出してでも要るなら追い出す"""
        capacity = self.capacity
        if capacity == 0:
            return None
        if self._free:
            return self._free.pop()
        if len(self._frames) < capacity:
            return self._allocate()
        worst = self._worst()
        if self._rank(worst) <= self._rank(frame):
            # 取ってある絵の方が値打ちがある 追い出してまで置く価値が無い
            # ここで無理に置くと、すぐ使う絵を捨てて遠い絵を貯めることになる
            # **捨てる側と同じものさしで比べる** 遠さだけで比べると、
            # 同じ遠さの後ろの絵が「捨てる 1 枚」に選ばれているのに、
            # それを追い出して前の絵を置くことができない
            return None
        surface = self._frames.pop(worst)
        return surface

    def _allocate(self) -> CacheSurface | None:
        """描画先を 1 枚増やす 取れなければ、そこを上限として諦める

        投げたまま上へ返すと、先読みの都合でプレビューが落ちる
        取れた所までで貯めれば、絵は出続ける
        """
        try:
            return self._make(self._width, self._height)
        except (ShaderError, GLError):
            # GPU のメモリが足りない フレームバッファが組めない形で返ることも、
            # 確保そのものが GL のエラーになることもある どちらも同じ扱い
            self._ceiling = len(self._frames)
            return None

    def _worst(self) -> int:
        """次に捨てる 1 枚"""
        return max(self._frames, key=self._rank)

    def _rank(self, frame: int) -> tuple[int, bool]:
        """捨てる順のものさし 大きいほど先に捨てる

        遠さが同じときは**後ろを先に捨てる** 遠さだけで比べると、同じ遠さの
        2 枚のうち先に入れた方が残り、捨てる順が入れた順で決まってしまう
        """
        return (self._cost(frame), frame < self._playhead)

    def _cost(self, frame: int) -> int:
        """再生ヘッドからの遠さ 大きいほど先に捨てる"""
        if frame >= self._playhead:
            return frame - self._playhead
        return (self._playhead - frame) * BACKWARD_WEIGHT

    def _recycle(self, frames: list[int]) -> None:
        for frame in frames:
            surface = self._frames.pop(frame, None)
            if surface is not None:
                self._free.append(surface)

    @staticmethod
    def _release(surfaces: Iterable[CacheSurface]) -> None:
        for surface in list(surfaces):
            surface.release()


class PreviewCache:
    """レンダラと :class:`FrameCache` をつないで、プレビューへ出す

    当たれば転送するだけ 外れたらその場で描き、**描いた絵は取っておく**
    行き来しながら詰める編集では、1 度描いた所へすぐ戻ってくる
    """

    def __init__(self, renderer: FrameRenderer, *, budget_bytes: int) -> None:
        self._renderer = renderer
        width, height = renderer.size
        self._cache = FrameCache(width, height, budget_bytes=budget_bytes)

    @property
    def cache(self) -> FrameCache:
        return self._cache

    @property
    def enabled(self) -> bool:
        return self._cache.capacity > 0

    def set_budget(self, budget_bytes: int) -> None:
        self._cache.set_budget(budget_bytes)

    def invalidate(self, invalidation: Invalidation) -> int:
        return self._cache.invalidate(invalidation)

    def clear(self) -> None:
        self._cache.clear()

    def release(self) -> None:
        self._cache.release()

    def draw(self, frame: int, framebuffer: int, viewport: tuple[int, int, int, int]) -> bool:
        """``frame`` を出す 取ってあった絵で済んだなら ``True``

        描く大きさが変わっていたらここで合わせる レンダラの画質設定を変えた
        直後に古い大きさの絵を出すと、余白の付き方が変わって絵が跳ねる
        """
        self._cache.resize(*self._renderer.size)
        self._cache.set_playhead(frame)
        surface = self._cache.get(frame)
        hit = surface is not None
        if surface is None:
            surface = self._cache.store(frame, lambda target: self._compose_into(frame, target))
        if surface is None:
            # 置く場所が無い 取っておかずにそのまま画面へ出す
            self._renderer.compose(frame)
            self._renderer.compositor.present(framebuffer, viewport)
            return False
        self._renderer.compositor.show(surface.color, framebuffer, viewport)
        return hit

    def step(self, playhead: int) -> bool:
        """先の 1 コマを描いて取っておく 描いたら ``True``

        1 回の呼び出しで 1 コマだけにする まとめて描くと、その間ずっと
        画面が固まる（描いているのは編集画面と同じ GL コンテキスト）
        """
        self._cache.resize(*self._renderer.size)
        self._cache.set_playhead(playhead)
        frame = self._next(playhead)
        if frame is None:
            return False
        surface = self._cache.store(frame, lambda target: self._compose_into(frame, target))
        return surface is not None

    def _next(self, playhead: int) -> int | None:
        """次に描くコマ 再生ヘッドから前へ、まだ無い所を探す"""
        capacity = self._cache.capacity
        if capacity == 0:
            return None
        duration = self._renderer.project.duration
        start = max(0, playhead)
        cached = self._cache.cached
        for frame in range(start, min(duration, start + capacity)):
            if frame not in cached:
                return frame
        return None

    def _compose_into(self, frame: int, surface: CacheSurface) -> None:
        self._renderer.compose(frame)
        # 余白を付けずに、取っておく絵いっぱいへ書く 付けてしまうと、
        # 出すときにもう 1 度余白が付いて内側へ縮む
        self._renderer.compositor.present(
            surface.handle, (0, 0, surface.width, surface.height), letterbox=False
        )
