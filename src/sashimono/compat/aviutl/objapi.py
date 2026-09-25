"""AviUtl の ``obj`` API

AviUtl のスクリプトは、1 つのオブジェクト（画像バッファ + 描画パラメータ）を
書き換えることで効果を作る ここではその「オブジェクト」を :class:`ObjectState`
として持ち、Lua から触れる窓口を :class:`ObjApi` が提供する

**描画は 2 段になっている** スクリプトが :meth:`ObjApi.draw` を呼ばなければ、
実行後の状態で 1 回だけ描かれる（AviUtl と同じ） 呼べばその回数だけ描かれ、
自動描画は行われない 移動やコピーを作るスクリプトはこの仕組みで動いている

画像は RGBA の uint8、ストレートアルファ AviUtl は内部で BGRA だが、ここで
合わせると読み書きのたびに並べ替えが要るので、境界（``getpixel`` など）でだけ
AviUtl の見え方に合わせる
"""

from __future__ import annotations

import codecs
import math
import random
import re
from collections.abc import Buffer
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from sashimono.compat.aviutl import raster
from sashimono.compat.aviutl.native import PixelData
from sashimono.compat.aviutl.report import CompatibilityReport, global_report

__all__ = ["LUA_ENCODING", "DrawCall", "EffectRequest", "ObjApi", "ObjectState", "lua_text"]


#: Lua とやり取りする文字の符号化の名前 lupa のランタイムにこの名前を渡す
#:
#: 中身は UTF-8 で、読めないバイトだけを ``surrogateescape``（U+DC80〜U+DCFF の 1 文字）
#: で持つ lupa の既定の UTF-8 は読めないバイトで例外を出すので、ダイアログの
#: ``"\255"`` のような値は、Python から Lua へ渡す所でも、スクリプトが Python の
#: 窓口（``obj.名前 = 値`` など）へ返す所でも落ちていた ふつうの UTF-8 の文字
#: （日本語など）は、どちらの向きも UTF-8 そのままで変わらない
LUA_ENCODING = "sashimono_lua_bytes"


def _lua_codec(name: str) -> codecs.CodecInfo | None:
    if name != LUA_ENCODING:
        return None
    utf8 = codecs.lookup("utf-8")

    def encode(text: str, errors: str = "strict") -> tuple[bytes, int]:
        del errors
        return utf8.encode(text, "surrogateescape")

    def decode(data: Buffer, errors: str = "strict") -> tuple[str, int]:
        del errors
        return utf8.decode(data, "surrogateescape")

    return codecs.CodecInfo(encode=encode, decode=decode, name=LUA_ENCODING)


codecs.register(_lua_codec)

#: ``surrogateescape`` で表せない代用符号 U+DC80〜U+DCFF の外にあるもの
_FOREIGN_SURROGATE = re.compile("[\ud800-\udc7f\udd00-\udfff]")


def lua_text(value: Any, report: CompatibilityReport | None = None) -> Any:
    """Lua へ渡す値 バイトに戻せない文字だけを置き換える

    読めないバイト（U+DC80〜U+DCFF）は :data:`LUA_ENCODING` がそのままのバイトで渡す
    それ以外の対になっていない代用符号（``\\ud800`` など）は、どのバイトにも
    戻せないので lupa へ渡す所で例外になり、スクリプトの失敗としても扱われずに
    描画ごと落ちる U+FFFD に置き換え、置き換えたことを記録に残す
    """
    if isinstance(value, str) and _FOREIGN_SURROGATE.search(value):
        (report or global_report).note_missing("Lua へ渡せない文字（対になっていない代用符号）")
        return _FOREIGN_SURROGATE.sub("\ufffd", value)
    return value


#: AviUtl のフィルタ名と、こちらのエフェクト種別の対応
#: 名前が同じでも中身は完全には一致しない 見た目の系統を合わせるための対応表
EFFECT_NAMES: dict[str, str] = {
    "ぼかし": "blur",
    "発光": "glow",
    "グロー": "glow",
    "色調補正": "color",
    "クロマキー": "chroma_key",
    "縁取り": "border",
    "影": "shadow",
    "シャドー": "shadow",
    "シャープ": "sharpen",
    "ノイズ": "noise",
    "モザイク": "mosaic",
    "マスク": "mask",
    "領域拡張": "crop",
    "クリッピング": "crop",
}

#: AviUtl の図形名と、こちらの図形の対応
FIGURE_NAMES: dict[str, str] = {
    "円": "ellipse",
    "四角形": "rect",
    "三角形": "triangle",
    "五角形": "pentagon",
    "六角形": "hexagon",
    "星形": "star",
    "背景": "background",
}


@dataclass(frozen=True, slots=True)
class EffectRequest:
    """``obj.effect`` で頼まれたフィルタ"""

    kind: str
    params: dict[str, float | str]
    #: AviUtl での呼ばれ方 記録に残すため
    original: str = ""


@dataclass(slots=True)
class DrawCall:
    """1 回分の描画

    値の意味は AviUtl に合わせる 位置は画面中央からのずれ、``rz`` は度、
    ``zoom`` は 1.0 が等倍、``aspect`` は正で横が縮む
    """

    image: np.ndarray
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    zoom: float = 1.0
    alpha: float = 1.0
    rx: float = 0.0
    ry: float = 0.0
    rz: float = 0.0
    aspect: float = 0.0
    sx: float = 1.0
    sy: float = 1.0
    cx: float = 0.0
    cy: float = 0.0
    cz: float = 0.0
    effects: tuple[EffectRequest, ...] = ()
    #: ``obj.drawpoly`` の四隅（画面中央からの ``x, y, z``、左上・右上・右下・左下）
    #: これがあるときは位置・回転・拡大を使わず、この四角形へ貼る
    quad: tuple[tuple[float, float, float], ...] | None = None
    #: 四隅に対応する絵の中の位置（画素） ``None`` なら絵全体
    uv: tuple[tuple[float, float], ...] | None = None


@dataclass(slots=True)
class ObjectState:
    """スクリプトが触る 1 オブジェクト"""

    image: np.ndarray
    #: 画面（プロジェクト）の大きさ
    screen_w: int = 1920
    screen_h: int = 1080
    #: オブジェクトの中での位置 AviUtl と同じで 0 始まり
    frame: int = 0
    totalframe: int = 1
    framerate: float = 30.0
    layer: int = 0
    index: int = 0
    num: int = 1

    ox: float = 0.0
    oy: float = 0.0
    oz: float = 0.0
    rx: float = 0.0
    ry: float = 0.0
    rz: float = 0.0
    cx: float = 0.0
    cy: float = 0.0
    cz: float = 0.0
    zoom: float = 1.0
    alpha: float = 1.0
    aspect: float = 0.0
    #: 軸ごとの倍率 拡張描画のスクリプトが使う 1.0 が等倍
    sx: float = 1.0
    sy: float = 1.0
    sz: float = 1.0

    #: ``obj.track0`` … の値
    track: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0, 0.0])
    check0: bool = False
    #: 名前付きパラメータ（``--track@`` や ``--dialog`` で作られたもの）
    values: dict[str, Any] = field(default_factory=dict)

    #: 明示的に呼ばれた描画 空なら実行後に 1 回だけ自動で描く
    draws: list[DrawCall] = field(default_factory=list)
    #: 積まれたフィルタ 描くときに GPU で掛ける 間に絵を読む・変える呼び出しが来たら
    #: その場で掛けて空にする（:meth:`ObjApi._settle_effects`）
    effects: list[EffectRequest] = field(default_factory=list)
    #: ``obj.load("buffer")`` などで使う作業用バッファ
    buffers: dict[str, np.ndarray] = field(default_factory=dict)
    #: ``obj.setfont`` で決めた書体 ``obj.mes`` が使う
    font: dict[str, Any] = field(default_factory=dict)
    #: ``obj.setoption`` で設定した描画オプション
    options: dict[str, Any] = field(default_factory=dict)
    #: :attr:`image` を描画の記録やバッファと共有している 画素を書き換える前に
    #: 複製する 描くたびに複製すると、何十回も描くスクリプトで画像の数だけ写す
    image_shared: bool = False

    @property
    def width(self) -> int:
        return int(self.image.shape[1])

    @property
    def height(self) -> int:
        return int(self.image.shape[0])

    @property
    def time(self) -> float:
        return self.frame / self.framerate if self.framerate else 0.0

    @property
    def totaltime(self) -> float:
        return self.totalframe / self.framerate if self.framerate else 0.0

    def snapshot(self) -> DrawCall:
        """いまの状態を 1 回分の描画にする"""
        self.image_shared = True
        return DrawCall(
            image=self.image,
            x=self.ox,
            y=self.oy,
            z=self.oz,
            zoom=self.zoom,
            alpha=self.alpha,
            rx=self.rx,
            ry=self.ry,
            rz=self.rz,
            aspect=self.aspect,
            sx=self.sx,
            sy=self.sy,
            cx=self.cx,
            cy=self.cy,
            cz=self.cz,
            effects=tuple(self.effects),
        )

    def writable_image(self) -> np.ndarray:
        """画素を書き換えてよい画像 共有していれば先に複製する

        共有したまま書くと、記録済みの描画まで最後の状態で描かれる
        """
        if self.image_shared:
            self.image = self.image.copy()
            self.image_shared = False
        return self.image

    def result(self) -> tuple[DrawCall, ...]:
        """描画の一覧 明示的な描画が無ければ自動描画を 1 つ"""
        return tuple(self.draws) if self.draws else (self.snapshot(),)


#: 書き換えられる値 ここに無い名前への代入は無視して記録する
_WRITABLE = frozenset(
    {
        "ox",
        "oy",
        "oz",
        "rx",
        "ry",
        "rz",
        "cx",
        "cy",
        "cz",
        "zoom",
        "alpha",
        "aspect",
        # 軸ごとの倍率 拡張描画のスクリプトがこれで縦横を別々に伸ばす
        "sx",
        "sy",
        "sz",
    }
)

#: 読み出し専用の値
_READABLE = frozenset(
    {
        "x",
        "y",
        "z",
        "w",
        "h",
        "screen_w",
        "screen_h",
        "framerate",
        "frame",
        "time",
        "totalframe",
        "totaltime",
        "layer",
        "index",
        "num",
        "check0",
    }
)

#: 番号として返すだけのもの AviUtl では内部の識別子
_IDENTIFIERS = {"id", "effect_id", "objectindex"}


class ObjApi:
    """Lua から見える ``obj``

    ここに無い名前が呼ばれたら、落とさずに :class:`CompatibilityReport` へ記録して
    ``nil`` を返す スクリプト 1 つが動かないことより、何が足りないのかが
    分かることを優先する
    """

    def __init__(
        self,
        state: ObjectState,
        *,
        report: CompatibilityReport | None = None,
        script: str = "",
        render_source: Any = None,
        load_module: Any = None,
        load_script_module: Any = None,
        emit: Any = None,
        apply_effects: Any = None,
    ) -> None:
        self.state = state
        self._report = report if report is not None else global_report
        self._script = script
        #: テキストや図形を絵にする関数 Qt に依存するので外から渡す
        self._render_source = render_source
        #: 共通処理のファイルを読む関数 ランタイムが渡す
        self._load_module = load_module
        #: ``obj.module`` の実体（``.mod2`` を読む） 無ければ ``require`` と同じ物を使う
        self._load_script_module = load_script_module or load_module
        #: テキスト欄に埋め込んだ Lua を走らせているときの書き出し先
        #: そこでの ``obj.mes`` は絵を作るのではなく、本文を書き出す
        self._emit = emit
        #: 積んだ効果をいまの絵へ掛けて返す関数 ``(絵, 効果の組) -> 絵`` 効果は GPU で
        #: 掛けるので外から渡す 無ければ焼き込めない（:meth:`_settle_effects`）
        self._apply_effects = apply_effects
        #: この実行で焼き込んだ回数（:data:`MAX_BAKES`）
        self._bakes = 0
        #: 積んだ効果の数が上限（:data:`MAX_STACKED_EFFECTS`）を越えたことを記録済みか
        self._effects_overflowed = False
        self._random = random.Random(0)

    # --- 値の読み書き ---

    def get(self, name: str) -> Any:
        """``obj.名前`` の読み出し"""
        state = self.state
        if name in _WRITABLE:
            return getattr(state, name)
        if name == "w":
            return state.width
        if name == "h":
            return state.height
        if name in ("x", "y", "z"):
            # 表示基準座標 こちらでは中央を原点にしているので 0
            return 0.0
        if name in _READABLE:
            return getattr(state, name)
        if name.startswith("track") and name[5:].isdigit():
            slot = int(name[5:])
            return state.track[slot] if slot < len(state.track) else 0.0
        if name.startswith("check") and name[5:].isdigit():
            return 1 if state.check0 else 0
        if name in state.values:
            return lua_text(state.values[name], self._report)
        if name in _IDENTIFIERS:
            return state.index

        method = getattr(self, f"lua_{name}", None)
        if method is not None:
            return method

        self._report.note_missing(f"obj.{name}")
        return None

    def set(self, name: str, value: Any) -> None:
        """``obj.名前 = 値`` の代入"""
        if name in _WRITABLE:
            setattr(self.state, name, _as_float(value))
            return
        if name in _READABLE or name in ("w", "h"):
            # AviUtl でも読み出し専用 黙って捨てず、記録に残す
            self._report.note_missing(f"obj.{name} への代入")
            return
        self.state.values[name] = value

    # --- 描画 ---

    def lua_draw(
        self,
        x: float | None = None,
        y: float | None = None,
        z: float | None = None,
        zoom: float | None = None,
        alpha: float | None = None,
        rx: float | None = None,
        ry: float | None = None,
        rz: float | None = None,
    ) -> None:
        """いまの画像を 1 回描く 引数を省くと現在の値を使う"""
        state = self.state
        if self._drawing_to_tempbuffer():
            self._draw_to_tempbuffer(x, y, z, zoom, alpha, rx, ry, rz)
            return
        call = state.snapshot()
        if x is not None:
            call.x = _as_float(x)
        if y is not None:
            call.y = _as_float(y)
        if z is not None:
            call.z = _as_float(z)
        if zoom is not None:
            call.zoom = _as_float(zoom)
        if alpha is not None:
            call.alpha = _as_float(alpha)
        if rx is not None:
            call.rx = _as_float(rx)
        if ry is not None:
            call.ry = _as_float(ry)
        if rz is not None:
            call.rz = _as_float(rz)
        state.draws.append(call)

    def lua_drawpoly(self, *args: Any) -> None:
        """四隅を指定して描く

        ``obj.drawpoly(x0,y0,z0, x1,y1,z1, x2,y2,z2, x3,y3,z3 [,u0,v0, …, u3,v3] [,alpha])``
        四隅は左上・右上・右下・左下の順で、オブジェクトの位置（``ox`` ``oy`` ``oz``）
        からの相対 ``u`` ``v`` は絵の中の画素で、省くと絵全体を貼る

        位置以外の描画パラメータ（回転・拡大）は掛けない 四隅そのものが形を
        決めるので、掛けると二重に変形する
        """
        if args and _is_table(args[0]):
            self._drawpoly_table(args[0], args[1:])
            return
        values = [_as_float(value) for value in args]
        if len(values) < 12:
            self._report.note_missing("obj.drawpoly（四隅が足りない）")
            return

        if self._drawing_to_tempbuffer():
            # 仮想バッファへはここで CPU が貼る 画面へ描くときに掛かる効果は、ここでは掛からない
            self._settle_effects("obj.drawpoly")
            corners = [(values[i], values[i + 1]) for i in range(0, 12, 3)]
            rest = values[12:]
            uvs = None
            if len(rest) >= 8:
                # 数を並べる形の u, v は画素（表の形は 0〜1） 仮想バッファへは 0〜1 で渡す
                image = self.state.image
                uvs = [
                    (rest[i] / max(1, image.shape[1]), rest[i + 1] / max(1, image.shape[0]))
                    for i in range(0, 8, 2)
                ]
                rest = rest[8:]
            alpha = rest[0] if rest else 1.0
            full = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
            self._fill_quad(corners, uvs or full, None, alpha)
            return

        state = self.state
        quad = tuple(
            (values[i] + state.ox, values[i + 1] + state.oy, values[i + 2] + state.oz)
            for i in range(0, 12, 3)
        )
        uv: tuple[tuple[float, float], ...] | None = None
        alpha = 1.0
        rest = values[12:]
        if len(rest) not in (0, 1, 8, 9):
            # 途中で切れた UV を透明度として読むと、絵が薄くなったり消えたりする
            self._report.note_missing("obj.drawpoly（UV か透明度の引数の数が合わない）")
            # 先頭の UV の値を透明度として読むと、0 のとき板ごと消える
            rest = [] if len(rest) < 8 else rest[:9]
        if len(rest) >= 8:
            uv = tuple((rest[i], rest[i + 1]) for i in range(0, 8, 2))
            rest = rest[8:]
        if rest:
            alpha = rest[0]

        call = state.snapshot()
        call.quad = quad
        call.uv = uv
        call.alpha = state.alpha * alpha
        state.draws.append(call)

    # --- 仮想バッファ ---

    def _drawing_to_tempbuffer(self) -> bool:
        return str(self.state.options.get("drawtarget", "framebuffer")) == "tempbuffer"

    def _tempbuffer(self) -> np.ndarray:
        """描く先の仮想バッファ まだ無ければ画面の大きさで作る

        仕様書では大きさを省くと「初期化しない」 前に作った物が無いときは、
        何も描かれていない画面と同じ大きさの透明な物として扱う
        """
        state = self.state
        buffer = state.buffers.get("tmp")
        if buffer is None:
            buffer = np.zeros((state.screen_h, state.screen_w, 4), dtype=np.uint8)
            state.buffers["tmp"] = buffer
        return buffer

    def _draw_to_tempbuffer(self, *values: float | None) -> None:
        """``obj.draw`` を仮想バッファへ 座標は引数のまま（オブジェクトの位置は使わない）

        **オブジェクトの透明度（``obj.alpha``）も掛けない** 仕様書は「オブジェクトの
        持っている座標等の設定は反映せず」とする 仮想バッファへ描いてから
        ``obj.load("tempbuffer")`` で読み戻す形（テレビ字幕もこれ）では、読み戻した
        絵を最後に描くときに ``obj.alpha`` が掛かる ここでも掛けると 2 回掛かり、
        透明度 50% が 25% になる 掛けるのは引数で渡された透明度だけ
        """
        x, y, _z, zoom, alpha, rx, ry, rz = values
        if any(value not in (None, 0, 0.0) for value in (rx, ry, rz)) or (
            zoom is not None and _as_float(zoom) != 1.0
        ):
            # 回したり拡げたりして仮想バッファへ描くのは、まだ写していない
            # 黙って等倍で描くと、形の違う絵ができても気づけない
            self._report.note_missing("obj.draw（仮想バッファへ、回転か拡大つき）")
            return
        # 画面へ描くときに掛かる効果は、CPU が貼る仮想バッファには掛からない 先に掛けておく
        self._settle_effects("obj.draw")
        raster.draw_image(
            self._tempbuffer(),
            self.state.image,
            _as_float(x) if x is not None else 0.0,
            _as_float(y) if y is not None else 0.0,
            _as_float(alpha) if alpha is not None else 1.0,
        )

    def _drawpoly_table(self, table: Any, rest: tuple[Any, ...]) -> None:
        """``obj.drawpoly({表}[, 頂点の数, 透明度])`` 頂点の表を並べた形

        仕様書（lua.txt）の 2 つの形のうち、頂点を 1 つずつ表にした形
        （``{x,y,z,u,v}`` か ``{x,y,z,r,g,b,a}``）を扱う 四隅の数を並べた表
        （``{x0,y0,z0,…}``）は、中の表の長さで見分けて記録に残す
        """
        vertices = [_table_values(item) for item in _table_items(table)]
        if not vertices:
            return
        width = len(vertices[0])
        if width not in (5, 7) or any(len(vertex) != width for vertex in vertices):
            self._report.note_missing(f"obj.drawpoly（表の形 {width} 個ずつ）")
            return
        count = int(_as_float(rest[0])) if rest else 4
        alpha = _as_float(rest[1]) if len(rest) > 1 else 1.0
        if count not in (3, 4) or len(vertices) % count != 0:
            self._report.note_missing(f"obj.drawpoly（面の頂点の数 {count}）")
            return
        if not self._drawing_to_tempbuffer():
            # 画面へ直に描く方は、四角形を 1 つずつ今までの描画へ渡す
            # 三角形と頂点の色は、画面へ描く側がまだ持っていない
            if count != 4 or width != 5:
                self._report.note_missing("obj.drawpoly（画面へ、三角形か頂点の色）")
                return
            for face in range(0, len(vertices), 4):
                self._drawpoly_quad_to_screen(vertices[face : face + 4], alpha)
            return
        self._settle_effects("obj.drawpoly")
        for face in range(0, len(vertices), count):
            points = vertices[face : face + count]
            corners = [(vertex[0], vertex[1]) for vertex in points]
            if width == 5:
                self._fill_polygon(corners, [(v[3], v[4]) for v in points], None, alpha)
            else:
                colors = [(v[3], v[4], v[5], v[6]) for v in points]
                self._fill_polygon(corners, None, colors, alpha)

    def _drawpoly_quad_to_screen(self, vertices: list[list[float]], alpha: float) -> None:
        state = self.state
        image = state.image
        call = state.snapshot()
        call.quad = tuple(
            (vertex[0] + state.ox, vertex[1] + state.oy, vertex[2] + state.oz)
            for vertex in vertices
        )
        # 表の形の u, v は 0〜1 今までの描画は画素で受ける
        call.uv = tuple(
            (vertex[3] * image.shape[1], vertex[4] * image.shape[0]) for vertex in vertices
        )
        call.alpha = state.alpha * alpha
        state.draws.append(call)

    def _fill_polygon(
        self,
        corners: list[tuple[float, float]],
        uvs: list[tuple[float, float]] | None,
        colors: list[tuple[float, float, float, float]] | None,
        alpha: float,
    ) -> None:
        if len(corners) == 4:
            self._fill_quad(corners, uvs, colors, alpha)
            return
        raster.draw_triangle(
            self._tempbuffer(),
            tuple(corners),
            texture=self.state.image,
            uvs=tuple(uvs) if uvs is not None else None,
            colors=tuple(colors) if colors is not None else None,
            alpha=alpha,
        )

    def _fill_quad(
        self,
        corners: list[tuple[float, float]],
        uvs: list[tuple[float, float]] | None,
        colors: list[tuple[float, float, float, float]] | None,
        alpha: float,
    ) -> None:
        """四角形を 2 つの三角形に分けて描く（0-1-2 と 0-2-3）"""
        for first, second, third in ((0, 1, 2), (0, 2, 3)):
            raster.draw_triangle(
                self._tempbuffer(),
                (corners[first], corners[second], corners[third]),
                texture=self.state.image,
                uvs=(uvs[first], uvs[second], uvs[third]) if uvs is not None else None,
                colors=(colors[first], colors[second], colors[third])
                if colors is not None
                else None,
                alpha=alpha,
            )

    def lua_effect(self, *args: Any) -> None:
        """フィルタを積む ``obj.effect("ぼかし", "範囲", 20)``"""
        if not args:
            self._report.note_missing("obj.effect（引数なし）")
            return
        original = str(args[0])
        if original == OFFSCREEN_EFFECT:
            self._offscreen()
            return
        if original == RESIZE_EFFECT:
            self._resize(args[1:])
            return
        kind = EFFECT_NAMES.get(original)
        if kind is None:
            self._report.note_missing(f"obj.effect({original})")
            return

        params: dict[str, float | str] = {}
        for index in range(1, len(args) - 1, 2):
            params[str(args[index])] = _as_param(args[index + 1])
        if len(self.state.effects) >= MAX_STACKED_EFFECTS:
            # 積んだ効果は描くときに 1 つずつ GPU のパスになる 焼き込みの上限を越えた後や、
            # 焼き込まずに積み続けるスクリプトでは列が命令数の上限まで伸び、1 コマが止まる
            # 越えた分は捨てる 黙ると効果が掛からない理由が分からないので 1 度だけ記録する
            if not self._effects_overflowed:
                self._effects_overflowed = True
                self._report.note_missing(
                    f"obj.effect({original})（積んだ効果が {MAX_STACKED_EFFECTS} 個を越えた）"
                )
            return
        self.state.effects.append(EffectRequest(kind=kind, params=params, original=original))

    def _offscreen(self) -> None:
        """``obj.effect("オフスクリーン描画")``

        それまでに積んだ効果を絵へ焼き込む物 **積んだ効果が無いときは絵が
        変わらない**（配布物のテレビ字幕は、文字の直後にこれを呼んでいて、この形しか
        確かめていない）
        """
        self._settle_effects("obj.effect(オフスクリーン描画)")

    def _resize(self, args: tuple[Any, ...]) -> None:
        """``obj.effect("リサイズ", "X", 120, "Y", 40, "ドット数でサイズ指定", 1)``

        描画のときではなく、ここで絵の大きさを変える 後に続く ``obj.w`` や
        ``obj.copybuffer`` が変えた後の大きさを見るため sigma の 単純図形σ は
        1 画素の四角を読んでリサイズで幅と高さにする 描くときまで待つと、その間の
        処理が 1 画素の絵を相手にする（以前は X と Y を位置のずれとして GPU へ渡していて、
        1 画素のまま横へずれた）

        ``ドット数でサイズ指定`` が真なら X と Y は画素 偽なら 拡大率 と X・Y の百分率を
        掛ける ``補間なし`` が真なら最も近い画素を取る
        """
        values: dict[str, float] = {}
        for index in range(0, len(args) - 1, 2):
            values[str(args[index])] = _as_float(args[index + 1])
        # 先に積んだ効果は広げる前の絵へ掛ける ぼかしてから広げればぼけ幅も広がる 描くときまで
        # 待つと広げた後の絵へ掛かり、ぼけ幅も縁の太さも元のままになる（#176） 大きさは焼き込んだ
        # 後の絵から測る AviUtl のぼかしは絵を広げるので、拡大率は広がった絵に掛かる
        self._settle_effects("obj.effect(リサイズ)")
        state = self.state
        if values.get("ドット数でサイズ指定", 0.0):
            width = values.get("X", float(state.width))
            height = values.get("Y", float(state.height))
        else:
            zoom = values.get("拡大率", 100.0) / 100.0
            width = state.width * zoom * values.get("X", 100.0) / 100.0
            height = state.height * zoom * values.get("Y", 100.0) / 100.0
        if not (math.isfinite(width) and math.isfinite(height)):
            self._report.note_missing("obj.effect(リサイズ) の大きさ（数ではない）")
            return
        if max(round(width), round(height)) > MAX_FIGURE_SIZE:
            # 大きさはスクリプトが決める そのまま作ると 1 回で数 GB になるので切るが、
            # 黙って切ると要求より小さく描かれた理由が互換性レポートに出ない
            self._report.note_missing(
                f"obj.effect(リサイズ) の大きさ {round(width)}x{round(height)}"
                f"（上限 {MAX_FIGURE_SIZE} で切った）"
            )
        size = (
            max(1, min(round(width), MAX_FIGURE_SIZE)),
            max(1, min(round(height), MAX_FIGURE_SIZE)),
        )
        state.image = raster.resize(state.image, *size, smooth=not values.get("補間なし", 0.0))
        state.image_shared = False

    def _settle_effects(self, caller: str) -> None:
        """積んだ効果をいまの絵へ掛けてしまう その場で絵を読む・変える呼び出しの前に呼ぶ

        AviUtl の ``obj.effect`` はその場で絵を変える こちらは積んでおいて描くときに GPU で
        まとめて掛けるので、間に絵を読む・変える呼び出しが挟まると順が入れ替わる
        （ぼかし → リサイズ が リサイズ → ぼかし になる #176） 挟まったときだけここで掛ける
        いつも掛けると、効果を積んで描くだけのスクリプトまで 1 回ごとに GPU から読み戻す

        掛ける関数が無い（GPU を持たない試験や道具）ときは焼き込めない 黙ると順が入れ替わった
        理由が分からないので記録に残し、効果は描くときに掛かるまま残す
        """
        state = self.state
        if not state.effects:
            return
        if self._apply_effects is None:
            self._report.note_missing(f"{caller}（先に積んだ効果の焼き込み）")
            return
        if self._bakes >= MAX_BAKES:
            # 1 回ごとに GPU で掛けて読み戻す 効果を積んでは画素を読む繰り返しを許すと、Lua の
            # 命令数の上限の内でも 1 コマが止まるほど重くなる 上限を越えたら焼き込まず、効果は
            # 描くときに掛かるまま残す 黙ると順が違う理由が分からないので 1 度だけ記録する
            if self._bakes == MAX_BAKES:
                self._report.note_missing(
                    f"{caller}（焼き込みが 1 回の実行で {MAX_BAKES} 回を越えた）"
                )
                self._bakes += 1
            return
        self._bakes += 1
        original = state.image
        baked = self._apply_effects(original, tuple(state.effects))
        # 掛ける物が無い（範囲 0 のぼかしなど）と同じ配列が返る そのときに共有の印を消すと、
        # 後の putpixel が記録済みの描画やバッファの絵へ直に書く
        if baked is not original:
            state.image = baked
            state.image_shared = False
        state.effects.clear()

    def _drop_effects(self) -> None:
        """絵を差し替える呼び出しの前に、積んだ効果を捨てる

        AviUtl では効果は差し替える前の絵に掛かって、絵ごと消える 残すと、差し替えた後の
        絵へ描くときに掛かり、読み込んだ図形や文字が前の絵のためのぼかしでぼける
        """
        self.state.effects.clear()

    def lua_filter(self, *args: Any) -> None:
        del args
        self._report.note_missing("obj.filter")

    # --- 画像の用意 ---

    def lua_load(self, kind: str = "", *args: Any) -> None:
        """画像バッファを差し替える"""
        name = str(kind)
        if name == "figure":
            self._load_figure(args)
        elif name == "text":
            self._load_text(args)
        elif name in ("buffer", "tmp", "tempbuffer", "object"):
            self._load_buffer((name,) if name != "buffer" else args)
        elif name in ("image", "movie"):
            self._report.note_missing(f'obj.load("{name}")')
        else:
            self._report.note_missing(f'obj.load("{name}")')

    def _load_figure(self, args: tuple[Any, ...]) -> None:
        """``obj.load("figure", 名前, 色, サイズ, 線幅)``"""
        if self._render_source is None:
            self._report.note_missing('obj.load("figure")')
            return
        figure = FIGURE_NAMES.get(str(args[0]) if args else "円", "ellipse")
        color = _color_of(args[1] if len(args) > 1 else 0xFFFFFF)
        size = _as_float(args[2]) if len(args) > 2 else 100.0
        line = _as_float(args[3]) if len(args) > 3 else 0.0

        state = self.state
        if figure == "background":
            width, height = state.screen_w, state.screen_h
            figure = "rect"
        else:
            if not math.isfinite(size):
                # int(NaN) は例外になり、スクリプト全体が止まる
                self._report.note_missing(f'obj.load("figure") の大きさ {size!r}（数ではない）')
                size = 100.0
            if size > MAX_FIGURE_SIZE:
                # 大きさはスクリプトが決める そのまま画像を作ると 1 回で数 GB になる
                self._report.note_missing(
                    f'obj.load("figure") の大きさ {int(size)}（上限で切った）'
                )
            width = height = max(1, min(int(size), MAX_FIGURE_SIZE))
        if line >= min(FILLED_LINE, max(width, height)):
            # 図形より太い線は塗りつぶし sigma の 楕円 は 8000 を渡して塗りつぶしを頼む
            # 輪郭として内側へ引くと、太さが図形を越えて何も残らず、円が透明になる
            line = 0.0
        canvas = self._render_source(
            "shape",
            {
                "shape": figure,
                "width": float(width),
                "height": float(height),
                "color": color,
                "line_width": line,
                "outline_only": line > 0,
                # 図形オブジェクトと同じ描き方をする API なので、輪郭も内側（#87）
                "line_align": "inside",
            },
            _same_parity(max(width, state.screen_w), width),
            _same_parity(max(height, state.screen_h), height),
        )
        # 図形の大きさちょうどの絵にする 描く側は画面の大きさの真ん中に描いて返す
        # 画面の大きさのまま持つと ``obj.w`` が画面の幅になり、図形を 0〜1 で
        # 貼るスクリプト（テレビ字幕の板）が、ほとんど透明な所を貼って板が消える
        top = max(0, (canvas.shape[0] - height) // 2)
        left = max(0, (canvas.shape[1] - width) // 2)
        self._drop_effects()
        self.state.image = np.ascontiguousarray(canvas[top : top + height, left : left + width])

    def _load_text(self, args: tuple[Any, ...]) -> None:
        """``obj.load("text", 本文)`` 書体は :meth:`lua_setfont` の指定に従う"""
        self.lua_mes(str(args[0]) if args else "")

    def _load_buffer(self, args: tuple[Any, ...]) -> None:
        name = _buffer_name(str(args[0]) if args else "tmp")
        stored = self.state.buffers.get(name)
        if stored is None:
            self._report.note_missing(f'obj.load("buffer", "{name}")')
            return
        self._drop_effects()
        self.state.image = stored
        self.state.image_shared = True

    def lua_copybuffer(self, destination: str = "", source: str = "") -> None:
        """バッファをコピーする ``obj.copybuffer("tmp", "obj")``

        名前の付け方は世代でぶれる（``obj`` ``object`` ``tmp`` ``tempbuffer``）
        同じものを指す綴りは同じ扱いにする
        """
        state = self.state
        origin_name = _buffer_name(str(source))
        target_name = _buffer_name(str(destination))
        if origin_name == "obj" and target_name != "obj":
            # 写す絵は効果を掛けた後の絵 sigma は 領域拡張 や 縁取り の直後に写して取っておく
            self._settle_effects("obj.copybuffer")

        origin = state.image if origin_name == "obj" else state.buffers.get(origin_name)
        if origin is None:
            self._report.note_missing(f'obj.copybuffer(source="{source}")')
            return
        if target_name == "obj":
            if origin_name != "obj":
                self._drop_effects()
            state.image = origin.copy()
            state.image_shared = False
            return
        if target_name not in state.buffers and len(state.buffers) >= MAX_BUFFERS:
            # 名前はスクリプトが決める 毎回違う名前で写されると、画面 1 枚ぶんずつ溜まる
            self._report.note_missing("obj.copybuffer（バッファの数が上限を超えた）")
            return
        state.buffers[target_name] = origin.copy()

    def lua_mes(self, text: Any = "") -> None:
        """テキストを描く ``obj.mes`` と ``obj.load("text", …)`` の実体"""
        if self._emit is not None:
            # テキスト欄の中の Lua から呼ばれている ここで絵にすると、
            # 1x1 の作業用の絵へ描いて捨てることになり、本文が空になる
            # （合成フォントの配布エイリアスは ``obj.mes`` で本文を出す）
            #
            # 値はそのまま渡す 文字にするのは Lua の ``tostring`` の仕事
            # Python の ``str`` で文字にすると、``true`` が ``True``、
            # ``nil`` が ``None`` と出て、同じテキスト欄の ``mes`` と食い違う
            self._emit(text)
            return
        if self._render_source is None:
            self._report.note_missing("obj.mes")
            return
        font = self.state.font
        size = float(font.get("size", 48))
        params = {
            "text": str(text),
            "size": size,
            "font": str(font.get("name", "Yu Gothic UI")),
            "color": _color_of(font.get("color", 0xFFFFFF)),
            "bold": bool(font.get("bold", False)),
            "italic": bool(font.get("italic", False)),
        }
        width = self.state.screen_w
        height = self.state.screen_h
        self._drop_effects()
        self.state.image = self._render_source("text", params, width, height)

    def lua_setfont(self, name: str = "", size: float = 48, *rest: Any) -> None:
        """書体を決める

        引数の数は世代で違う AviUtl1 は ``(名前, サイズ, 装飾, 色, 影色)`` の
        5 つだが、AviUtl2 の配布スクリプトは字間や行間まで渡してくる
        余分は受けて無視する 数が合わないというだけで落とすのは損が大きい
        """
        style = int(_as_float(rest[0])) if len(rest) > 0 else 0
        color = rest[1] if len(rest) > 1 else 0xFFFFFF
        self.state.font = {
            "name": str(name) or "Yu Gothic UI",
            "size": _as_float(size),
            "bold": bool(style & 1),
            "italic": bool(style & 2),
            "color": color,
            # ``obj.getfont`` が返せるように、受けた引数をそのまま覚えておく
            # 装飾や色を作り直すと、AviUtl では往復しても変わらない値が変わる
            "given": (str(name), _as_float(size), *rest),
            "extra": rest[2:],
        }

    def lua_getfont(self) -> Any:
        """いまの書体 ``obj.setfont`` の引数と同じ並びで返す

        AviUtl2 の ``lua.txt`` にある通り 9 つ返す
        ``名前, 大きさ, 装飾, 文字色, 影・縁色, 太字, 斜体, 字間, 行間``
        名前の初期値は**空**（＝既定の書体） 合成フォントの配布エイリアスは
        この名前をそのままプラグインへ渡すので、勝手に既定の書体名を入れると
        AviUtl2 と違う組み方になる

        ``obj.setfont`` で渡された値を覚えておいて返す 作り直すと、
        受け取っていない引数に既定値が入って往復で値が変わる
        """
        given = self.state.font.get("given")
        values: list[Any] = list(given) if isinstance(given, tuple) else ["", 48.0]
        # 足りない分は AviUtl の既定で埋める 短い組を返すと、受け側の
        # ``local a,b,c,d,e,f,g,h = obj.getfont()`` に nil が並ぶ
        defaults: list[Any] = ["", 48.0, 0, 0xFFFFFF, 0x000000, False, False, 0.0, 0.0]
        values.extend(defaults[len(values) :])
        return tuple(values[: len(defaults)])

    # --- 画素 ---

    def lua_getpixel(self, x: int = 0, y: int = 0, kind: str = "col") -> Any:
        """1 画素を読む ``kind`` が ``"col"`` なら ``色, 不透明度``"""
        self._settle_effects("obj.getpixel")
        image = self.state.image
        column, row = int(_as_float(x)), int(_as_float(y))
        if not (0 <= row < image.shape[0] and 0 <= column < image.shape[1]):
            return (0, 0.0)
        red, green, blue, alpha = (int(v) for v in image[row, column])
        if str(kind) == "rgb":
            return (red, green, blue, alpha / 255.0)
        return ((red << 16) | (green << 8) | blue, alpha / 255.0)

    def lua_putpixel(self, x: int = 0, y: int = 0, *values: Any) -> None:
        # 先に積んだ効果の上へ書く 後で掛けると、書いた画素までぼける
        self._settle_effects("obj.putpixel")
        image = self.state.writable_image()
        column, row = int(_as_float(x)), int(_as_float(y))
        if not (0 <= row < image.shape[0] and 0 <= column < image.shape[1]):
            return
        if len(values) >= 4:
            channels = [int(_as_float(v)) for v in values[:3]]
            alpha = int(_as_float(values[3]) * 255)
        elif values:
            color = int(_as_float(values[0]))
            channels = [(color >> 16) & 0xFF, (color >> 8) & 0xFF, color & 0xFF]
            alpha = int(_as_float(values[1]) * 255) if len(values) > 1 else 255
        else:
            return
        image[row, column] = [*channels, max(0, min(255, alpha))]

    def lua_copypixel(self, dx: int, dy: int, sx: int, sy: int) -> None:
        self._settle_effects("obj.copypixel")
        image = self.state.writable_image()
        target = (int(_as_float(dy)), int(_as_float(dx)))
        origin = (int(_as_float(sy)), int(_as_float(sx)))
        if _inside(image, target) and _inside(image, origin):
            image[target] = image[origin]

    def lua_getpixeldata(self, target: str = "object", order: str = "rgba") -> Any:
        """画像を読む 返すのは ``(データ, 幅, 高さ)``

        仕様書（lua.txt）どおり、データはスクリプトモジュール（DLL）へ渡すための物で、
        Lua からは中身を読めない こちらでも中身の見えない値として渡す
        """
        image = self._pixel_source(str(target))
        if image is None:
            return (None, 0, 0)
        pixels = image[..., [2, 1, 0, 3]] if str(order).lower() == "bgra" else image
        data = PixelData(pixels.copy(), "bgra" if str(order).lower() == "bgra" else "rgba")
        return (data, data.width, data.height)

    def lua_putpixeldata(
        self, target: str = "object", data: Any = None, width: Any = 0, height: Any = 0, *rest: Any
    ) -> None:
        """画像を書く ``obj.getpixeldata`` で受けたデータ（DLL が書き換えた物）を戻す"""
        if not isinstance(data, PixelData):
            self._report.note_missing("obj.putpixeldata（getpixeldata で受けた以外のデータ）")
            return
        w, h = int(_as_float(width)), int(_as_float(height))
        if (w, h) != (data.width, data.height):
            # 大きさを変えて書くと、別の並びとして読むことになり絵が崩れる
            self._report.note_missing("obj.putpixeldata（受けたときと違う大きさ）")
            return
        order = str(rest[0]).lower() if rest else data.order
        pixels = data.pixels[..., [2, 1, 0, 3]] if order == "bgra" else data.pixels
        name = str(target)
        key = _buffer_name(name)
        if key == "obj":
            # 書き戻す絵は getpixeldata で効果を掛け終えた物 残した効果をもう 1 度掛けない
            self._drop_effects()
            self.state.image = np.ascontiguousarray(pixels.copy())
            self.state.image_shared = False
        elif key == "tmp" or key.startswith("cache:"):
            self.state.buffers[key] = np.ascontiguousarray(pixels.copy())
        else:
            self._report.note_missing(f'obj.putpixeldata("{name}")')

    def _pixel_source(self, target: str) -> np.ndarray | None:
        key = _buffer_name(target)
        if key == "obj":
            # DLL が受け取るのは効果を掛けた後の絵 掛ける前の絵を渡すと、
            # 書き戻したときに効果ごと消える
            self._settle_effects("obj.getpixeldata")
            return self.state.image
        if key == "tmp" or key.startswith("cache:"):
            stored = self.state.buffers.get(key)
            if stored is None:
                self._report.note_missing(f'obj.getpixeldata("{target}")（まだ無いバッファ）')
            return stored
        # フレームバッファ（それまでに描いた画面）は、スクリプトの中からは見えない
        self._report.note_missing(f'obj.getpixeldata("{target}")')
        return None

    def lua_pixeloption(self, *args: Any) -> None:
        del args
        self._report.note_missing("obj.pixeloption")

    # --- 設定と情報 ---

    def lua_setoption(self, name: str = "", *values: Any) -> None:
        key = str(name)
        self.state.options[key] = values[0] if values else True
        if key == "drawtarget" and values and str(values[0]) == "tempbuffer" and len(values) >= 3:
            # 大きさを渡されたら、その大きさの透明な物で作り直す（仕様書どおり）
            asked = (_as_float(values[1]), _as_float(values[2]))
            if not all(math.isfinite(value) for value in asked):
                self._report.note_missing("obj.setoption（仮想バッファの大きさが数ではない）")
                return
            width = max(1, min(int(asked[0]), MAX_FIGURE_SIZE))
            height = max(1, min(int(asked[1]), MAX_FIGURE_SIZE))
            if (width, height) != (int(asked[0]), int(asked[1])):
                # 大きさはスクリプトが決める そのまま作ると 1 回で数 GB になりうるので
                # 上限で切るが、黙って切ると指定どおりに描けたように見える
                self._report.note_missing(
                    f"obj.setoption（仮想バッファの大きさ {int(asked[0])}x{int(asked[1])} を"
                    f" {width}x{height} に切った）"
                )
            self.state.buffers["tmp"] = np.zeros((height, width, 4), dtype=np.uint8)
        if key not in ("drawtarget", "blend", "focus_mode", "culling", "billboard"):
            self._report.note_missing(f'obj.setoption("{key}")')

    def lua_getoption(self, name: str = "", *args: Any) -> Any:
        del args
        key = str(name)
        if key == "camera_mode":
            return 0
        if key == "gui":
            return False
        return self.state.options.get(key)

    def lua_setanchor(self, *args: Any) -> None:
        """アンカーの表示 画面上の操作なので、値だけ受けて記録する"""
        del args
        self._report.note_missing("obj.setanchor")

    def lua_getvalue(self, target: str = "", *args: Any) -> Any:
        """設定値を読む ``obj.getvalue("track0")`` など

        AviUtl2 のスクリプトは ``obj.getvalue("track.名前")`` とも書く
        前置きを外して同じものを引く
        """
        del args
        name = str(target)
        for candidate in (name, name.split(".", 1)[-1]):
            if candidate in self.state.values:
                return lua_text(self.state.values[candidate], self._report)
            value = self.get(candidate)
            if value is not None and not callable(value):
                return value
        self._report.note_missing(f'obj.getvalue("{name}")')
        return 0.0

    def lua_getpoint(self, *args: Any) -> Any:
        del args
        self._report.note_missing("obj.getpoint")
        return 0

    def lua_getinfo(self, name: str = "", *args: Any) -> Any:
        """環境の情報"""
        del args
        key = str(name)
        state = self.state
        if key == "script_path":
            return ""
        if key == "saving":
            return False
        if key == "image_max":
            return (state.screen_w, state.screen_h)
        if key == "screen_size":
            return (state.screen_w, state.screen_h)
        self._report.note_missing(f'obj.getinfo("{key}")')
        return None

    def lua_getaudio(self, *args: Any) -> Any:
        del args
        self._report.note_missing("obj.getaudio")
        return 0

    def lua_rand(
        self, minimum: int = 0, maximum: int = 1, seed: int | None = None, frame: int | None = None
    ) -> int:
        """同じフレームなら同じ値になる乱数

        毎回ばらつくと、1 フレーム描き直すたびに絵が変わってしまう 種は
        オブジェクトと時間から作る
        """
        low, high = int(_as_float(minimum)), int(_as_float(maximum))
        if low > high:
            low, high = high, low
        base = int(_as_float(seed)) if seed is not None else self.state.index
        step = int(_as_float(frame)) if frame is not None else self.state.frame
        self._random.seed(hash((base, step, low, high)))
        return self._random.randint(low, high)

    def lua_interpolation(self, *args: Any) -> Any:
        """連続した点の間を補間する ``obj.interpolation(t, x0,y0, x1,y1, …)``"""
        if len(args) < 3:
            return 0.0
        position = _as_float(args[0])
        points = [_as_float(value) for value in args[1:]]
        return _catmull_rom(position, points)

    def lua_pixelshader(self, *args: Any) -> None:
        del args
        self._report.note_missing("obj.pixelshader")

    def lua_computeshader(self, *args: Any) -> None:
        del args
        self._report.note_missing("obj.computeshader")

    def lua_module(self, name: str = "", *args: Any) -> Any:
        """``obj.module`` — 共通処理のファイルを読む

        配布スクリプトは処理を ``.mod2`` へ切り出していることが多い 読めないと
        本体の 1 行目で落ちるので、ここが通ることの意味は大きい
        """
        del args
        if self._load_script_module is None:
            self._report.note_missing("obj.module")
            return None
        return self._load_script_module(str(name))

    def lua_multiobject(self, *args: Any) -> None:
        del args
        self._report.note_missing("obj.multiobject")


#: ``obj.load("figure")`` の一辺の上限（画素） AviUtl の既定の最大画像サイズより大きい
MAX_FIGURE_SIZE = 4096

#: 名前付きバッファの数の上限 AviUtl の配布スクリプトが使うのは tmp と数個の cache だけ
MAX_BUFFERS = 16

#: 1 回の実行で積んだ効果を焼き込む回数の上限（:meth:`ObjApi._settle_effects`）
#: 実物の配布物は 1 つの処理で数回（sigma の 縁取り → 写す → 単色化 → 写す など）
MAX_BAKES = 64

#: 焼き込まずに積んでおける効果の数の上限（:meth:`ObjApi.lua_effect`） 1 つずつ描くときの
#: GPU のパスになる 実物の配布物が 1 度に積むのは数個（sigma の 単色化 → ぼかし など）
MAX_STACKED_EFFECTS = 32


#: それまでに積んだ効果を絵へ焼き込むフィルタの名前
OFFSCREEN_EFFECT = "オフスクリーン描画"

#: 絵の大きさをその場で変えるフィルタの名前（:meth:`ObjApi._resize`）
RESIZE_EFFECT = "リサイズ"

#: ``obj.load("figure")`` の線の太さがこれ以上なら塗りつぶし 図形オブジェクトの読み込み
#: （:mod:`sashimono.compat.aviutl.mapping`）と同じ決まり AviUtl2 の既定値がこの値
FILLED_LINE = 4000.0


def _is_table(value: Any) -> bool:
    """Lua の表か Lua から来た値は、表なら ``items`` を持つ"""
    return not isinstance(value, str | bytes) and callable(getattr(value, "items", None))


def _table_items(table: Any) -> list[Any]:
    """配列として並んだ中身（1 から途切れずに続く所）"""
    items: list[Any] = []
    index = 1
    while True:
        value = table[index] if _has(table, index) else None
        if value is None:
            return items
        items.append(value)
        index += 1


def _has(table: Any, key: Any) -> bool:
    try:
        return table[key] is not None
    except (KeyError, IndexError, TypeError):
        return False


def _table_values(value: Any) -> list[float]:
    if not _is_table(value):
        return []
    return [_as_float(item) for item in _table_items(value)]


def _buffer_name(name: str) -> str:
    """バッファ名を揃える

    AviUtl は同じものを ``obj`` とも ``object`` とも呼ぶ 綴りの違いだけで
    「バッファが無い」と言われても直しようがない
    """
    cleaned = name.strip().lower()
    if cleaned in ("", "obj", "object"):
        return "obj"
    if cleaned in ("tmp", "temp", "tempbuffer"):
        return "tmp"
    return cleaned


def _inside(image: np.ndarray, position: tuple[int, int]) -> bool:
    row, column = position
    return bool(0 <= row < image.shape[0] and 0 <= column < image.shape[1])


def _same_parity(canvas: int, figure: int) -> int:
    """図形を真ん中に描く絵の大きさ 図形と偶奇をそろえる

    偶奇が違うと図形の端が画素の真ん中に来て、縁が半透明ににじむ 1 画素の四角
    （sigma の 矩形 がリサイズで広げる元）は 4 画素に 1/4 ずつ散って、どこも不透明にならない
    """
    return canvas + (canvas - figure) % 2


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _as_param(value: Any) -> float | str:
    if isinstance(value, str):
        return value
    return _as_float(value)


def _color_of(value: Any) -> tuple[float, float, float, float]:
    """AviUtl の ``0xRRGGBB`` を 0..1 の組へ"""
    number = int(_as_float(value))
    return (
        ((number >> 16) & 0xFF) / 255.0,
        ((number >> 8) & 0xFF) / 255.0,
        (number & 0xFF) / 255.0,
        1.0,
    )


def _catmull_rom(position: float, points: list[float]) -> float:
    """点の並びを滑らかに繋いだ曲線の値

    AviUtl の ``obj.interpolation`` は座標の組を受け取るが、ここでは 1 次元の
    値の並びとして扱う 移動の軌跡を作るのに使われるのが主で、その用途では
    軸ごとに呼ばれる
    """
    if not points:
        return 0.0
    if len(points) == 1:
        return points[0]

    span = len(points) - 1
    scaled = min(max(position, 0.0), 1.0) * span
    index = min(int(scaled), span - 1)
    t = scaled - index

    p0 = points[max(0, index - 1)]
    p1 = points[index]
    p2 = points[index + 1]
    p3 = points[min(span, index + 2)]
    return 0.5 * (
        (2 * p1)
        + (-p0 + p2) * t
        + (2 * p0 - 5 * p1 + 4 * p2 - p3) * t * t
        + (-p0 + 3 * p1 - 3 * p2 + p3) * t * t * t
    )


def rgb_to_number(red: float, green: float, blue: float) -> int:
    """``RGB()`` の実体"""
    return (
        (int(min(max(red, 0), 255)) << 16)
        | (int(min(max(green, 0), 255)) << 8)
        | int(min(max(blue, 0), 255))
    )


def hsv_to_number(hue: float, saturation: float, value: float) -> int:
    """``HSV()`` の実体 AviUtl と同じで H は 0..360、S と V は 0..255"""
    h = (float(hue) % 360.0) / 60.0
    s = min(max(float(saturation), 0.0), 255.0) / 255.0
    v = min(max(float(value), 0.0), 255.0)
    if s <= 0.0:
        level = int(v)
        return rgb_to_number(level, level, level)

    sector = math.floor(h)
    fraction = h - sector
    p = v * (1.0 - s)
    q = v * (1.0 - s * fraction)
    t = v * (1.0 - s * (1.0 - fraction))
    table = [(v, t, p), (q, v, p), (p, v, t), (p, q, v), (t, p, v), (v, p, q)]
    red, green, blue = table[sector % 6]
    return rgb_to_number(red, green, blue)
