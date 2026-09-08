"""プロジェクトの 1 フレームを合成する。

プレビューも書き出しもここを通る。同じ経路を使うことで「プレビューでは出るのに
書き出すと出ない」が構造的に起きない。
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from fractions import Fraction

import numpy as np

from novaedit.core.model import Clip, MediaId, Project, Track
from novaedit.core.timebase import FrameRate
from novaedit.engine.decode import ProbeError, VideoDecoder
from novaedit.engine.gpu import (
    Compositor,
    EffectProcessor,
    GLScope,
    OffscreenGLContext,
    Placement,
    Texture,
    fit_placement,
)
from novaedit.engine.sources import render_source

__all__ = ["FrameRenderer", "RenderQuality"]

#: 同時に開いておくデコーダの上限。素材ごとにコンテナとスレッドを抱えるので、
#: 際限なく開くとファイルハンドルとメモリを食い潰す。
MAX_OPEN_DECODERS = 8


@dataclass(frozen=True, slots=True)
class RenderQuality:
    """プレビューの解像度を落として再生を軽くするための設定。

    ``divisor`` が 2 なら縦横半分。合成そのものが軽くなるので、重いタイムラインでも
    実時間再生を維持できる。書き出しでは常に 1 を使う。
    """

    divisor: int = 1

    def __post_init__(self) -> None:
        if self.divisor < 1:
            raise ValueError(f"分母は 1 以上必要: {self.divisor}")

    def apply(self, width: int, height: int) -> tuple[int, int]:
        return max(1, width // self.divisor), max(1, height // self.divisor)


FULL_QUALITY = RenderQuality(1)


class FrameRenderer:
    """タイムラインの指定フレームを 1 枚の画像に合成する。

    GL コンテキストを持つので、生成したスレッドの上でだけ使うこと。再生用と
    書き出し用で別インスタンスにする。

    ``context`` を省略すると自前でオフスクリーンのコンテキストを作る。Qt の
    ウィジェットの中から使うときは、すでに current になっているので
    :class:`~novaedit.engine.gpu.CurrentGLContext` を渡す。
    """

    def __init__(
        self,
        project: Project,
        *,
        context: GLScope | None = None,
        quality: RenderQuality = FULL_QUALITY,
    ) -> None:
        self._project = project
        self._quality = quality
        self._owns_context = context is None
        self._context = context if context is not None else OffscreenGLContext()

        width, height = quality.apply(*project.settings.resolution)
        with self._context:
            self._compositor = Compositor(width, height)
            # エフェクト処理は合成と同じ全画面四角形を使い回す。
            self._effects = EffectProcessor(width, height, self._compositor.quad)
        #: 素材ごとのデコーダ。最近使ったものを残す。
        self._decoders: OrderedDict[tuple[MediaId, int], VideoDecoder] = OrderedDict()
        #: トラックごとの転送用テクスチャ。毎フレーム作り直すと確保と解放で時間を食う。
        self._textures: dict[str, Texture] = {}
        self._closed = False

    @property
    def project(self) -> Project:
        return self._project

    @property
    def size(self) -> tuple[int, int]:
        return self._compositor.width, self._compositor.height

    def set_project(self, project: Project) -> None:
        """編集後のプロジェクトに差し替える。

        解像度が変わればフレームバッファも作り直す。参照されなくなった素材の
        デコーダはここで閉じる。開いたままだとファイルを差し替えられない。
        """
        previous = self._project
        self._project = project

        if project.settings.resolution != previous.settings.resolution:
            self._resize(*self._quality.apply(*project.settings.resolution))

        alive = {m.id for m in project.media}
        for key in [k for k in self._decoders if k[0] not in alive]:
            self._decoders.pop(key).close()

    def set_quality(self, quality: RenderQuality) -> None:
        self._quality = quality
        self._resize(*quality.apply(*self._project.settings.resolution))

    def _resize(self, width: int, height: int) -> None:
        with self._context:
            self._compositor.resize(width, height)
            self._effects.resize(width, height)

    @property
    def compositor(self) -> Compositor:
        """合成結果を直接画面へ出したいときの逃げ道。プレビューが使う。"""
        return self._compositor

    def compose(self, frame: int) -> None:
        """``frame`` を合成する。結果は CPU へ戻さず GPU 上に残る。

        画面に出すだけなら往復が要らない。:meth:`render` はこれを呼んでから
        読み出しているだけ。
        """
        if self._closed:
            raise RuntimeError("閉じたレンダラは使えない")

        rate = self._project.rate
        self._compositor.begin()
        for track in self._project.timeline.video_tracks():
            if track.muted:
                continue
            clip = track.clip_at(frame)
            if clip is None or not clip.enabled:
                continue
            self._draw_clip(track, clip, frame, rate)

    def render(self, frame: int) -> np.ndarray:
        """``frame`` の合成結果を sRGB の ``(高さ, 幅, 4)`` uint8 で返す。

        映像トラックを下から順に重ねる。タイムラインの下のトラックが奥、
        上のトラックが手前という Premiere / AviUtl と同じ並び。
        """
        with self._context:
            self.compose(frame)
            return self._compositor.read()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for decoder in self._decoders.values():
            decoder.close()
        self._decoders.clear()
        with self._context:
            for texture in self._textures.values():
                texture.release()
            self._textures.clear()
            self._effects.release()
            self._compositor.release()
        if self._owns_context:
            self._context.release()

    def _draw_clip(self, track: Track, clip: Clip, frame: int, rate: FrameRate) -> None:
        image = self._image_for(clip, frame, rate)
        if image is None:
            return

        texture = self._texture_for(track.id)
        texture.upload(image)
        local_frame = frame - clip.timeline_start
        opacity = clip.opacity.at(local_frame)

        if not self._effects.has_work(clip.effects):
            # エフェクトが無ければ中間バッファを通さない。全画面のパスが 1 回
            # 増えるだけで、エフェクト無しのクリップでも再生の余裕が削られる。
            self._compositor.draw(texture, opacity=opacity, blend=clip.blend_mode)
            return

        placement = fit_placement(
            texture.width, texture.height, self._compositor.width, self._compositor.height
        )
        result = self._effects.apply(
            texture,
            clip.effects,
            frame=local_frame,
            fps=float(rate.fps),
            source_rect=placement.to_clip(self._compositor.width, self._compositor.height),
        )
        # エフェクトを通した結果は画面いっぱいで GL の向き。収め直しも反転も要らない。
        self._compositor.draw_handle(
            result.color,
            Placement(0.0, 0.0, float(self._compositor.width), float(self._compositor.height)),
            opacity=opacity,
            flip=False,
            blend=clip.blend_mode,
        )

    def _image_for(self, clip: Clip, frame: int, rate: FrameRate) -> np.ndarray | None:
        """クリップの元絵。素材由来と生成オブジェクトの両方をここで扱う。"""
        if clip.media_id is None:
            return self._generate(clip, frame, rate)
        return self._decode(clip, frame, rate)

    def _generate(self, clip: Clip, frame: int, rate: FrameRate) -> np.ndarray | None:
        """素材を持たないクリップ（テキスト・図形）の絵を作る。"""
        del rate
        if clip.source is None:
            return None
        return render_source(
            clip.source,
            self._compositor.width,
            self._compositor.height,
            frame=frame - clip.timeline_start,
        )

    def _decode(self, clip: Clip, frame: int, rate: FrameRate) -> np.ndarray | None:
        assert clip.media_id is not None
        media = self._project.find_media(clip.media_id)
        if media is None or not media.has_video:
            return None

        decoder = self._decoder_for(clip.media_id, clip.stream_index)
        if decoder is None:
            return None

        local_frame = frame - clip.timeline_start
        source_time = clip.source_in + local_frame * rate.frame_duration * clip.speed
        if media.is_still:
            # 静止画は時間を持たない。常に先頭を返す。
            source_time = Fraction(0)
        return decoder.frame_at(source_time)

    def _decoder_for(self, media_id: MediaId, stream_index: int) -> VideoDecoder | None:
        key = (media_id, stream_index)
        existing = self._decoders.get(key)
        if existing is not None:
            self._decoders.move_to_end(key)
            return existing

        media = self._project.find_media(media_id)
        if media is None:
            return None
        try:
            decoder = VideoDecoder(media.path, stream_index)
        except ProbeError:
            # オフライン素材や壊れたファイル。ここで落とすと、1 本壊れただけで
            # プロジェクト全体が開けなくなる。そのクリップだけ映らない扱いにする。
            return None

        self._decoders[key] = decoder
        while len(self._decoders) > MAX_OPEN_DECODERS:
            _, evicted = self._decoders.popitem(last=False)
            evicted.close()
        return decoder

    def _texture_for(self, key: str) -> Texture:
        texture = self._textures.get(key)
        if texture is None:
            texture = Texture(1, 1)
            self._textures[key] = texture
        return texture
