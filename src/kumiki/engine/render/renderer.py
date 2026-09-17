"""プロジェクトの 1 フレームを合成する

プレビューも書き出しもここを通る 同じ経路を使うことで「プレビューでは出るのに
書き出すと出ない」が構造的に起きない
"""

from __future__ import annotations

import math
from collections import OrderedDict
from dataclasses import dataclass
from fractions import Fraction

import numpy as np
from OpenGL import GL

from kumiki.compat.aviutl.embedded import has_embedded
from kumiki.compat.aviutl.report import global_report
from kumiki.core.model import (
    AnimatedValue,
    Clip,
    Effect,
    MediaId,
    Project,
    Timeline,
    Track,
    TrackKind,
)
from kumiki.core.timebase import FrameRate, seconds_to_frame
from kumiki.effects.easing import ease
from kumiki.engine.decode import ProbeError, VideoDecoder
from kumiki.engine.gpu import (
    BlendMode,
    Compositor,
    Corners,
    EffectProcessor,
    Framebuffer,
    GLScope,
    OffscreenGLContext,
    Placement,
    Texture,
    Transform,
    fit_placement,
)
from kumiki.engine.gpu.projection import project
from kumiki.engine.render.scripts import (
    ScriptStage,
    requested_effects,
    script_catalog,
    script_effects,
    split_effects,
)
from kumiki.engine.sources import render_source, source_canvas

__all__ = ["FrameRenderer", "RenderQuality"]


def _content_box(image: np.ndarray) -> tuple[int, int, int, int] | None:
    """絵の中で色が付いている範囲（画素、左・上・右・下） 何も無ければ ``None``

    テキストや図形は画面と同じ大きさの絵で届く 角丸や中心基準の動きは、この範囲を
    絵の大きさとして扱わないと、画面の角や画面の中央を基準にしてしまう
    """
    alpha = image[..., 3]
    columns = np.flatnonzero(alpha.any(axis=0))
    rows = np.flatnonzero(alpha.any(axis=1))
    if columns.size == 0 or rows.size == 0:
        return None
    return int(columns[0]), int(rows[0]), int(columns[-1]) + 1, int(rows[-1]) + 1


def _placed_bounds(
    box: tuple[int, int, int, int] | None,
    image: np.ndarray | None,
    placement: Placement,
    image_width: int = 0,
    image_height: int = 0,
) -> tuple[float, float, float, float] | None:
    """絵の中の範囲を、置いた先（画面の画素）の範囲へ写す"""
    if box is None:
        return None
    if image is not None:
        image_height, image_width = int(image.shape[0]), int(image.shape[1])
    scale_x = placement.width / max(image_width, 1)
    scale_y = placement.height / max(image_height, 1)
    left, top, right, bottom = box
    return (
        placement.left + left * scale_x,
        placement.top + top * scale_y,
        placement.left + right * scale_x,
        placement.top + bottom * scale_y,
    )


def _as_corners(points: tuple[tuple[float, float], ...] | None) -> Corners | None:
    """4 点の組を四隅の型へ 数が合わなければ ``None``"""
    if points is None or len(points) != 4:
        return None
    return (points[0], points[1], points[2], points[3])


#: シーンの入れ子の深さの上限 循環はコマンドとファイルの読み込みで止めてあるが、
#: 深く積んだだけでも描画の手間が掛け算で増える 画面の外から来た壊れた
#: プロジェクトで固まらないための最後の歯止め
MAX_SCENE_DEPTH = 8

#: 同時に開いておくデコーダの上限 素材ごとにコンテナとスレッドを抱えるので、
#: 際限なく開くとファイルハンドルとメモリを食い潰す
MAX_OPEN_DECODERS = 8


@dataclass(frozen=True, slots=True)
class RenderQuality:
    """プレビューの解像度を落として再生を軽くするための設定

    ``divisor`` が 2 なら縦横半分 合成そのものが軽くなるので、重いタイムラインでも
    実時間再生を維持できる 書き出しでは常に 1 を使う
    """

    divisor: int = 1

    def __post_init__(self) -> None:
        if self.divisor < 1:
            raise ValueError(f"分母は 1 以上必要: {self.divisor}")

    def apply(self, width: int, height: int) -> tuple[int, int]:
        return max(1, width // self.divisor), max(1, height // self.divisor)


FULL_QUALITY = RenderQuality(1)


class FrameRenderer:
    """タイムラインの指定フレームを 1 枚の画像に合成する

    GL コンテキストを持つので、生成したスレッドの上でだけ使うこと 再生用と
    書き出し用で別インスタンスにする

    ``context`` を省略すると自前でオフスクリーンのコンテキストを作る Qt の
    ウィジェットの中から使うときは、すでに current になっているので
    :class:`~kumiki.engine.gpu.CurrentGLContext` を渡す
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
            # エフェクト処理は合成と同じ全画面四角形を使い回す
            self._effects = EffectProcessor(width, height, self._compositor.quad)
        #: 素材ごとのデコーダ 最近使ったものを残す
        self._decoders: OrderedDict[tuple[MediaId, int], VideoDecoder] = OrderedDict()
        #: トラックごとの転送用テクスチャ 毎フレーム作り直すと確保と解放で時間を食う
        self._textures: dict[str, Texture] = {}
        #: AviUtl スクリプトを走らせる係 使うまで作らない
        self._scripts: ScriptStage | None = None
        #: フレームバッファのクリップが画面を写し取る先 使うまで作らない
        self._grab: Framebuffer | None = None
        #: 入れ子のシーンを描く合成先 深さごとに 1 つ 使うまで作らない
        self._nested: dict[int, Compositor] = {}
        #: クリップを下のクリップの形で切り抜くときに使う合成先（役目と深さごと）
        self._layers: dict[tuple[str, int], Compositor] = {}
        self._closed = False

    @property
    def project(self) -> Project:
        return self._project

    @property
    def size(self) -> tuple[int, int]:
        return self._compositor.width, self._compositor.height

    def set_project(self, project: Project) -> None:
        """編集後のプロジェクトに差し替える

        解像度が変わればフレームバッファも作り直す 参照されなくなった素材の
        デコーダはここで閉じる 開いたままだとファイルを差し替えられない
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
        """合成結果を直接画面へ出したいときの逃げ道 プレビューが使う"""
        return self._compositor

    def compose(self, frame: int) -> None:
        """``frame`` を合成する 結果は CPU へ戻さず GPU 上に残る

        画面に出すだけなら往復が要らない :meth:`render` はこれを呼んでから
        読み出しているだけ
        """
        if self._closed:
            raise RuntimeError("閉じたレンダラは使えない")

        # 透明な下地の上で重ね、最後に黒を敷く 黒の上で重ねると、乗算などの合成が
        # 下に何も無い所でも黒と混ざる（YMM4 は透明な所では上の絵をそのまま出す）
        # 写し取る絵（フレームバッファ）も、YMM4 と同じく透明な下地のまま渡る
        self._compositor.begin((0.0, 0.0, 0.0, 0.0))
        self._compose_timeline(self._project.timeline, frame, depth=0)
        self._compositor.underlay((0.0, 0.0, 0.0, 1.0))

    def _compose_timeline(self, timeline: Timeline, frame: int, *, depth: int) -> None:
        """1 本のタイムラインを、いまの合成先へ重ねる シーンの入れ子でも同じ道を通る"""
        self._compose_tracks(list(timeline.active_tracks(TrackKind.VIDEO)), frame, depth)

    def _compose_tracks(self, tracks: list[Track], frame: int, depth: int) -> None:
        """下のトラックから順に重ねる 場面切り替えは、それより下のトラックを見て描き直す"""
        rate = self._project.rate
        visible = [
            (index, track, clip)
            for index, track in enumerate(tracks)
            if (clip := track.clip_at(frame)) is not None and clip.enabled
        ]
        below: Compositor | None = None
        for position, (index, track, clip) in enumerate(visible):
            if clip.source is not None and clip.source.kind == "transition":
                self._draw_transition(tracks[:index], clip, frame, rate, depth)
                below = None
                continue
            if clip.clip_to_below:
                self._draw_clipped(track, clip, frame, rate, depth, below)
            else:
                self._draw_clip(track, clip, frame, rate, depth)
            above = visible[position + 1][2] if position + 1 < len(visible) else None
            # すぐ上のクリップがこのクリップの形で切り抜くなら、形を取っておく
            below = (
                self._capture(track, clip, frame, rate, depth)
                if above is not None and above.clip_to_below
                else None
            )

    def _draw_transition(
        self, tracks: list[Track], clip: Clip, frame: int, rate: FrameRate, depth: int
    ) -> None:
        """下のトラックの絵を、前の場面から後の場面へ切り替える（YMM4 の ``TransitionItem``）

        決まりは YMM4 に描かせた試験（``tools/ymm4_probes.py`` の 4 回目）から読んだ

        - 後の場面は、いまの時刻の下の絵そのもの
        - 前の場面は、範囲の中で終わるクリップの終わり（切れ目）の直前で止めた絵
          切れ目より前は、前の場面も後の場面も同じ絵になる 範囲の中で終わるクリップが
          無ければ止めない
        - 進み具合は範囲の頭から終わりまで 押し出しとスライドは画面 1 枚分を動く
        - 切り替え（switch）は切れ目で入れ替わる（範囲の真ん中ではない）
        - 上のトラックには効かない
        """
        if depth >= MAX_SCENE_DEPTH:
            return
        start, end = clip.timeline_start, clip.timeline_end
        cut = max(
            (
                other.timeline_end
                for track in tracks
                for other in track.clips
                if other.enabled and start < other.timeline_end <= end
            ),
            default=end,
        )
        before_frame = min(frame, cut - 1)
        local_frame = frame - start
        params = clip.source.params if clip.source is not None else {}
        style = str(params.get("style", "fade"))
        target = str(params.get("target", "after"))
        angle = params.get("angle")
        degrees = angle.at(local_frame) if isinstance(angle, AnimatedValue) else 0.0
        progress = ease(
            local_frame / clip.duration,
            str(params.get("easing", "linear")),
            str(params.get("easing_mode", "in")),
        )

        width, height = self._compositor.width, self._compositor.height
        full = Placement(0.0, 0.0, float(width), float(height))
        outer = self._compositor
        after = self._layer("transition_after", depth)
        after.begin((0.0, 0.0, 0.0, 0.0))
        after.draw_handle(outer.canvas.color, full, flip=False, premultiplied=True)
        before = self._layer("transition_before", depth)
        self._compositor = before
        try:
            before.begin((0.0, 0.0, 0.0, 0.0))
            if before_frame == frame:
                before.draw_handle(after.canvas.color, full, flip=False, premultiplied=True)
            else:
                self._compose_tracks(tracks, before_frame, depth + 1)
        finally:
            self._compositor = outer

        before_image = self._transition_scene(
            before, clip.effects, "before", clip, local_frame, rate, depth
        )
        after_image = self._transition_scene(
            after, clip.after_effects, "after", clip, local_frame, rate, depth
        )

        outer.begin((0.0, 0.0, 0.0, 0.0))
        direction = (math.cos(math.radians(degrees)), math.sin(math.radians(degrees)))
        travel = abs(direction[0]) * width + abs(direction[1]) * height

        def moved(amount: float) -> Placement:
            # 角度 0 で右へ、90 で下へ（画面の Y は下が正）
            return Placement(
                direction[0] * amount, direction[1] * amount, float(width), float(height)
            )

        def put(
            image: Compositor,
            placement: Placement = full,
            opacity: float = 1.0,
            blend: str = BlendMode.NORMAL,
        ) -> None:
            outer.draw_handle(
                image.canvas.color,
                placement,
                opacity=opacity,
                flip=False,
                blend=blend,
                premultiplied=True,
            )

        if style == "switch":
            put(before_image if frame < cut else after_image)
        elif style == "fade":
            # YMM4 は黒の上の絵として sRGB の値のまま混ぜる リニアで混ぜると中間が明るく浮く
            put(before_image)
            outer.underlay((0.0, 0.0, 0.0, 1.0))
            put(after_image, opacity=progress, blend=BlendMode.SRGB_MIX)
        elif style == "push":
            put(after_image, moved(-(1.0 - progress) * travel))
            put(before_image, moved(progress * travel))
        elif style == "slide" and target == "before":
            put(after_image)
            put(before_image, moved(progress * travel))
        elif style == "slide":
            put(before_image)
            put(after_image, moved(-(1.0 - progress) * travel))
        elif target == "before":
            # 重ねるだけ 手前にする場面を後に描く
            put(after_image)
            put(before_image)
        else:
            put(before_image)
            put(after_image)

    def _transition_scene(
        self,
        image: Compositor,
        effects: tuple[Effect, ...],
        role: str,
        clip: Clip,
        local_frame: int,
        rate: FrameRate,
        depth: int,
    ) -> Compositor:
        """場面にエフェクトを掛けた絵 掛けるものが無ければそのまま返す

        エフェクトの結果は次に掛けるまでしか残らない（中のバッファを使い回す）ので、
        別の合成先へ描き写してから返す
        """
        gpu_effects, scripts = split_effects(effects)
        if scripts:
            global_report.note_missing("場面切り替えに積んだ AviUtl スクリプト")
        if not self._effects.has_work(gpu_effects):
            return image
        result = self._effects.apply(
            image.canvas,
            gpu_effects,
            frame=local_frame,
            fps=float(rate.fps),
            flip_source=False,
            duration=clip.duration,
            premultiplied=True,
        )
        done = self._layer(f"transition_{role}_done", depth)
        done.begin((0.0, 0.0, 0.0, 0.0))
        full = Placement(0.0, 0.0, float(done.width), float(done.height))
        done.draw_handle(result.color, full, flip=False)
        return done

    def render(self, frame: int) -> np.ndarray:
        """``frame`` の合成結果を sRGB の ``(高さ, 幅, 4)`` uint8 で返す

        映像トラックを下から順に重ねる タイムラインの下のトラックが奥、
        上のトラックが手前という Premiere / AviUtl と同じ並び
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
            if self._grab is not None:
                self._grab.release()
            for nested in (*self._nested.values(), *self._layers.values()):
                nested.release()
            self._nested.clear()
            self._layers.clear()
            self._compositor.release()
        if self._owns_context:
            self._context.release()

    def _draw_clip(
        self, track: Track, clip: Clip, frame: int, rate: FrameRate, depth: int = 0
    ) -> None:
        if clip.scene_id is not None:
            self._draw_scene(clip, frame, rate, depth)
            return
        if clip.source is not None and clip.source.kind == "framebuffer":
            self._draw_framebuffer(clip, frame, rate)
            return
        image = self._image_for(clip, frame, rate)
        if image is None:
            return

        local_frame = frame - clip.timeline_start
        opacity = clip.opacity.at(local_frame)
        gpu_effects, scripts = split_effects(clip.effects)

        if scripts:
            self._draw_scripted(track, clip, image, gpu_effects, local_frame, rate, opacity)
            return

        texture = self._texture_for(track.id)
        texture.upload(image)

        screen_width, screen_height = self._compositor.width, self._compositor.height
        if clip.media_id is None and (
            texture.width > screen_width or texture.height > screen_height
        ):
            # 画面より大きく作った生成オブジェクト 縮めて収めず、画面の中心に等倍で置く
            self._draw_oversized(texture, image, clip, gpu_effects, local_frame, rate, opacity)
            return

        if not self._effects.has_work(gpu_effects):
            # エフェクトが無ければ中間バッファを通さない 全画面のパスが 1 回
            # 増えるだけで、エフェクト無しのクリップでも再生の余裕が削られる
            self._compositor.draw(texture, opacity=opacity, blend=clip.blend_mode)
            return

        placement = fit_placement(
            texture.width, texture.height, self._compositor.width, self._compositor.height
        )
        result = self._effects.apply(
            texture,
            gpu_effects,
            frame=local_frame,
            fps=float(rate.fps),
            source_rect=placement.to_clip(self._compositor.width, self._compositor.height),
            duration=clip.duration,
            bounds=_placed_bounds(_content_box(image), image, placement),
        )
        # エフェクトを通した結果は画面いっぱいで GL の向き 収め直しも反転も要らない
        self._compositor.draw_handle(
            result.color,
            Placement(0.0, 0.0, float(self._compositor.width), float(self._compositor.height)),
            opacity=opacity,
            flip=False,
            blend=clip.blend_mode,
        )

    def _layer(self, role: str, depth: int) -> Compositor:
        """クリップ 1 本ぶんを描く透明な合成先 役目と入れ子の深さごとに使い回す"""
        width, height = self._compositor.width, self._compositor.height
        key = (role, depth)
        layer = self._layers.get(key)
        if layer is None:
            layer = Compositor(width, height)
            self._layers[key] = layer
        layer.resize(width, height)
        return layer

    def _draw_into(
        self, layer: Compositor, track: Track, clip: Clip, frame: int, rate: FrameRate, depth: int
    ) -> None:
        outer = self._compositor
        self._compositor = layer
        try:
            layer.begin((0.0, 0.0, 0.0, 0.0))
            self._draw_clip(track, clip, frame, rate, depth)
        finally:
            self._compositor = outer

    def _capture(
        self, track: Track, clip: Clip, frame: int, rate: FrameRate, depth: int
    ) -> Compositor:
        """上のクリップに切り抜きの形として使わせるため、このクリップだけを別に描く"""
        layer = self._layer("below", depth)
        self._draw_into(layer, track, clip, frame, rate, depth)
        return layer

    def _draw_clipped(
        self,
        track: Track,
        clip: Clip,
        frame: int,
        rate: FrameRate,
        depth: int,
        below: Compositor | None,
    ) -> None:
        """すぐ下のクリップの形で切り抜いて重ねる（YMM4 の「上のオブジェクトでクリッピング」）

        下にクリップが無ければ、切り抜く形が無いので何も見えない 合成モードは、切り抜く前の
        透明な合成先で当てる（下の絵とは通常で重ねる）
        """
        if below is None:
            return
        layer = self._layer("clipped", depth)
        self._draw_into(layer, track, clip, frame, rate, depth)
        full = Placement(0.0, 0.0, float(layer.width), float(layer.height))
        layer.draw_handle(
            below.canvas.color, full, flip=False, blend=BlendMode.MASK, premultiplied=True
        )
        self._compositor.draw_handle(layer.canvas.color, full, flip=False, premultiplied=True)

    def _draw_oversized(
        self,
        texture: Texture,
        image: np.ndarray,
        clip: Clip,
        gpu_effects: tuple[Effect, ...],
        local_frame: int,
        rate: FrameRate,
        opacity: float,
    ) -> None:
        """画面より大きい絵を、画面の中心に等倍で置く

        エフェクトは絵と同じ大きさのバッファで掛ける 画面の大きさのバッファへ先に
        置くと、そこで端が切れ、あとから回したり動かしたりしたときに切れ目が見える
        バッファの中心は画面の中心と同じなので、位置の計算（画素）は変わらない
        """
        screen_width, screen_height = self._compositor.width, self._compositor.height
        placed = Placement(
            (screen_width - texture.width) / 2.0,
            (screen_height - texture.height) / 2.0,
            float(texture.width),
            float(texture.height),
        )
        if not self._effects.has_work(gpu_effects):
            self._compositor.draw(texture, placement=placed, opacity=opacity, blend=clip.blend_mode)
            return
        self._effects.resize(texture.width, texture.height)
        try:
            box = _content_box(image)
            result = self._effects.apply(
                texture,
                gpu_effects,
                frame=local_frame,
                fps=float(rate.fps),
                duration=clip.duration,
                bounds=None
                if box is None
                else (float(box[0]), float(box[1]), float(box[2]), float(box[3])),
            )
            self._compositor.draw_handle(
                result.color, placed, opacity=opacity, flip=False, blend=clip.blend_mode
            )
        finally:
            self._effects.resize(screen_width, screen_height)

    def _draw_scene(self, clip: Clip, frame: int, rate: FrameRate, depth: int) -> None:
        """入れ子のシーンを別の合成先に描き、1 本のクリップとして重ねる

        シーンの中の時刻は、クリップの ``source_in``（秒）と速度で決まる 素材の
        クリップと同じ決まりなので、分割やトリムをしても中身がずれない

        シーンのキャンバスは透明から始める 黒で始めると、シーンを重ねた場所の
        下の絵が隠れる
        """
        scene = self._project.find_scene(clip.scene_id) if clip.scene_id else None
        if scene is None or depth >= MAX_SCENE_DEPTH:
            return
        local_frame = frame - clip.timeline_start
        # 別々に切り捨てると端数が 2 回落ち、1 フレーム前の絵になることがある
        elapsed = Fraction(local_frame) * rate.frame_duration * Fraction(clip.speed)
        scene_frame = seconds_to_frame(Fraction(clip.source_in) + elapsed, rate)

        width, height = self._compositor.width, self._compositor.height
        nested = self._nested.get(depth + 1)
        if nested is None:
            nested = Compositor(width, height)
            self._nested[depth + 1] = nested
        nested.resize(width, height)

        outer = self._compositor
        self._compositor = nested
        try:
            nested.begin((0.0, 0.0, 0.0, 0.0))
            self._compose_timeline(scene.timeline, scene_frame, depth=depth + 1)
        finally:
            self._compositor = outer

        gpu_effects, scripts = split_effects(clip.effects)
        if scripts:
            global_report.note_missing("シーンのクリップに積んだ AviUtl スクリプト")
        full = Placement(0.0, 0.0, float(width), float(height))
        opacity = clip.opacity.at(local_frame)
        if not self._effects.has_work(gpu_effects):
            outer.draw_handle(
                nested.canvas.color,
                full,
                opacity=opacity,
                flip=False,
                blend=clip.blend_mode,
                premultiplied=True,
            )
            return
        result = self._effects.apply(
            nested.canvas,
            gpu_effects,
            frame=local_frame,
            fps=float(rate.fps),
            flip_source=False,
            duration=clip.duration,
            premultiplied=True,
        )
        outer.draw_handle(result.color, full, opacity=opacity, flip=False, blend=clip.blend_mode)

    def _draw_framebuffer(self, clip: Clip, frame: int, rate: FrameRate) -> None:
        """それまでに重ねた画面を写し取り、エフェクトを掛けて重ねる

        キャンバスは描いている最中なので、そのまま読みながら同じキャンバスへ描くことは
        できない いったん別のバッファへ写す キャンバスは事前乗算アルファで溜まって
        いるので、そう伝えて渡す 合成は透明な下地から始まるので、伝えないと半透明の
        縁が暗くなる

        AviUtl スクリプトは掛けない スクリプトは CPU の画像を書き換える作りで、画面を
        毎フレーム CPU へ読み戻すと再生が追いつかない（積まれていれば記録に残す）
        """
        local_frame = frame - clip.timeline_start
        gpu_effects, scripts = split_effects(clip.effects)
        if scripts:
            global_report.note_missing("フレームバッファに積んだ AviUtl スクリプト")
        width, height = self._compositor.width, self._compositor.height
        with self._context:
            if self._grab is None:
                self._grab = Framebuffer(width, height)
            self._grab.resize(width, height)
            GL.glBindFramebuffer(GL.GL_READ_FRAMEBUFFER, self._compositor.canvas.handle)
            GL.glBindFramebuffer(GL.GL_DRAW_FRAMEBUFFER, self._grab.handle)
            GL.glBlitFramebuffer(
                0, 0, width, height, 0, 0, width, height, GL.GL_COLOR_BUFFER_BIT, GL.GL_NEAREST
            )
            # 写し取った画面は黒の上に置いた絵にする YMM4 は何も無い所も不透明な黒として
            # 写すので、反転すると白くなる 透明のまま渡すと反転しても黒のまま残る
            self._compositor.underlay((0.0, 0.0, 0.0, 1.0), target=self._grab)
            source: Framebuffer = self._grab
            if self._effects.has_work(gpu_effects):
                source = self._effects.apply(
                    self._grab,
                    gpu_effects,
                    frame=local_frame,
                    fps=float(rate.fps),
                    flip_source=False,
                    duration=clip.duration,
                    premultiplied=True,
                )
            self._compositor.draw_handle(
                source.color,
                Placement(0.0, 0.0, float(width), float(height)),
                opacity=clip.opacity.at(local_frame),
                flip=False,
                blend=clip.blend_mode,
                # エフェクトを通した結果はストレートアルファ 写しただけなら事前乗算のまま
                premultiplied=source is self._grab,
            )

    def _draw_scripted(
        self,
        track: Track,
        clip: Clip,
        image: np.ndarray,
        gpu_effects: tuple[Effect, ...],
        local_frame: int,
        rate: FrameRate,
        opacity: float,
    ) -> None:
        """AviUtl スクリプトを積んだクリップを描く

        スクリプトは「何回・どこへ・どう変形して描くか」を返す 1 回とは限らない
        （残像や複製を作るスクリプトがある）ので、返ってきた分だけ合成する
        """
        stage = self._script_stage()
        if stage is None:
            return

        calls = stage.run(
            clip,
            script_effects(clip.effects),
            image,
            frame=local_frame,
            fps=float(rate.fps),
        )
        texture = self._texture_for(track.id)
        width, height = self._compositor.width, self._compositor.height

        for call in calls:
            texture.upload(call.image)
            combined = gpu_effects + requested_effects(call)
            alpha = opacity * call.alpha

            if call.quad is not None:
                projected = [project(point, width, height) for point in call.quad]
                points = [point for point in projected if point is not None]
                if len(points) != 4:
                    # カメラを越えた隅がある 写せる隅だけで描くと形の違う板になる
                    continue
                uv = None
                if call.uv is not None:
                    uv = tuple(
                        (u / max(texture.width, 1), v / max(texture.height, 1)) for u, v in call.uv
                    )
                self._draw_on_quad(
                    texture,
                    (points[0], points[1], points[2], points[3]),
                    _as_corners(uv),
                    combined,
                    local_frame,
                    rate,
                    alpha,
                    clip.blend_mode,
                    clip.duration,
                    _content_box(call.image) if combined else None,
                )
                continue

            transform = Transform(
                x=call.x,
                y=call.y,
                zoom=call.zoom,
                rotation=call.rz,
                aspect=call.aspect,
                pivot_x=call.cx,
                pivot_y=call.cy,
                scale_x=call.sx,
                scale_y=call.sy,
                z=call.z,
                rotation_x=call.rx,
                rotation_y=call.ry,
            )
            if not transform.is_flat:
                corners = transform.corners(texture.width, texture.height, width, height)
                if corners is None:
                    continue
                self._draw_on_quad(
                    texture,
                    corners,
                    None,
                    combined,
                    local_frame,
                    rate,
                    alpha,
                    clip.blend_mode,
                    clip.duration,
                    _content_box(call.image) if combined else None,
                )
                continue

            placement = transform.placement(texture.width, texture.height, width, height)
            matrix = transform.matrix(width, height)

            if not self._effects.has_work(combined):
                self._compositor.draw(
                    texture,
                    placement=placement,
                    opacity=opacity * call.alpha,
                    blend=clip.blend_mode,
                    matrix=matrix,
                )
                continue

            result = self._effects.apply(
                texture,
                combined,
                frame=local_frame,
                fps=float(rate.fps),
                source_rect=placement.to_clip(self._compositor.width, self._compositor.height),
                duration=clip.duration,
                bounds=_placed_bounds(_content_box(call.image), call.image, placement),
            )
            self._compositor.draw_handle(
                result.color,
                Placement(0.0, 0.0, float(self._compositor.width), float(self._compositor.height)),
                opacity=opacity * call.alpha,
                flip=False,
                blend=clip.blend_mode,
                matrix=matrix,
            )

    def _draw_on_quad(
        self,
        texture: Texture,
        corners: Corners,
        uv: Corners | None,
        effects: tuple[Effect, ...],
        local_frame: int,
        rate: FrameRate,
        opacity: float,
        blend: str,
        duration: int = 0,
        box: tuple[int, int, int, int] | None = None,
    ) -> None:
        """絵を四角形へ貼る エフェクトがあれば、先に平らなまま掛けてから貼る

        エフェクトは画面と同じ大きさのバッファで動く 傾けてから掛けると、
        ぼかしや縁取りの幅まで遠近で歪む 平らな板に掛けてから板ごと傾けるのが
        AviUtl の見え方と同じ
        """
        width, height = self._compositor.width, self._compositor.height
        own = Placement(0.0, 0.0, float(texture.width), float(texture.height))
        if not self._effects.has_work(effects):
            self._compositor.draw_mapped(
                texture.handle,
                source=own,
                anchor=own,
                corners=corners,
                opacity=opacity,
                blend=blend,
                uv=uv,
            )
            return

        centred = Placement(
            (width - texture.width) / 2.0,
            (height - texture.height) / 2.0,
            float(texture.width),
            float(texture.height),
        )
        result = self._effects.apply(
            texture,
            effects,
            frame=local_frame,
            fps=float(rate.fps),
            source_rect=centred.to_clip(width, height),
            duration=duration,
            bounds=_placed_bounds(box, None, centred, texture.width, texture.height),
        )
        if uv is not None:
            # 絵の一部だけを貼るときは、切り出す四隅を結果のバッファの中の位置へ
            # 読み替えて貼る 外接矩形にまとめると、斜めに切り出した形が崩れる
            # 結果のバッファは GL の向き（下が 0）なので、縦は裏返す
            placed = tuple(
                (
                    (centred.left + u * texture.width) / width,
                    1.0 - (centred.top + v * texture.height) / height,
                )
                for u, v in uv
            )
            self._compositor.draw_mapped(
                result.color,
                source=own,
                anchor=own,
                corners=corners,
                opacity=opacity,
                blend=blend,
                uv=_as_corners(placed),
            )
            return
        self._compositor.draw_mapped(
            result.color,
            source=Placement(0.0, 0.0, float(width), float(height)),
            anchor=centred,
            corners=corners,
            opacity=opacity,
            flip=False,
            blend=blend,
        )

    def _script_stage(self) -> ScriptStage | None:
        """スクリプトを走らせる係 初めて必要になったときに作る

        Lua ランタイムの用意は数ミリ秒かかる スクリプトを使わない
        プロジェクトでその代金を払わせない
        """
        if self._scripts is None:
            self._scripts = ScriptStage(
                script_catalog(), screen=(self._compositor.width, self._compositor.height)
            )
        else:
            self._scripts.set_screen(self._compositor.width, self._compositor.height)
        return self._scripts

    def _image_for(self, clip: Clip, frame: int, rate: FrameRate) -> np.ndarray | None:
        """クリップの元絵 素材由来と生成オブジェクトの両方をここで扱う"""
        if clip.media_id is None:
            return self._generate(clip, frame, rate)
        return self._decode(clip, frame, rate)

    def _generate(self, clip: Clip, frame: int, rate: FrameRate) -> np.ndarray | None:
        """素材を持たないクリップ（テキスト・図形）の絵を作る"""
        source = clip.source
        if source is None:
            return None
        local_frame = frame - clip.timeline_start
        text = source.params.get("text") if source.kind == "text" else None
        if isinstance(text, str) and has_embedded(text):
            # 埋め込んだ Lua は描くたびに走らせる 時刻で数え上げる字幕のように、
            # フレームごとに文字が変わるため
            stage = self._script_stage()
            if stage is not None:
                expanded = stage.expand_text(
                    text, frame=local_frame, fps=float(rate.fps), duration=clip.duration
                )
                source = source.with_param("text", expanded)
        width, height = source_canvas(
            source, self._compositor.width, self._compositor.height, frame=local_frame
        )
        return render_source(source, width, height, frame=local_frame)

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
            # 静止画は時間を持たない 常に先頭を返す
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
            # オフライン素材や壊れたファイル ここで落とすと、1 本壊れただけで
            # プロジェクト全体が開けなくなる そのクリップだけ映らない扱いにする
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
