"""テキストと図形を絵にする

Qt の描画系（``QPainter``）を使う 日本語の禁則処理やフォントの字形選択、
縁取りの輪郭生成を自前で書くのは現実的ではなく、Qt はそれをすべて持っている

戻り値は常に sRGB・ストレートアルファの ``(高さ, 幅, 4)`` uint8 素材から
デコードした絵とまったく同じ形なので、この先の合成は区別せずに済む
"""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetricsF,
    QImage,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPen,
    QRadialGradient,
    QTransform,
)

from kumiki.core.model import AnimatedValue, GeneratedSource, ParamValue
from kumiki.effects.sources import SourceDefinition, source_registry

__all__ = ["render_source"]

#: 縦の基準ごとに、指定した位置より上へ出す割合 ``下`` なら全部が上に出る
_VERTICAL_SHARE = {"top": 0.0, "middle": 0.5, "bottom": 1.0}


def render_source(
    source: GeneratedSource, width: int, height: int, *, frame: int = 0, fps: float = 30.0
) -> np.ndarray | None:
    """生成オブジェクトを描いて配列で返す 未知の種類なら ``None``

    ``fps`` は時間で変わる図形（タイマー・集中線）がフレームを秒へ直すのに使う
    """
    definition = source_registry.get(source.kind)
    if definition is None:
        return None

    values = _resolve(definition, source.params, frame)
    values["_seconds"] = frame / max(fps, 1e-6)
    values["_fps"] = fps
    image = QImage(width, height, QImage.Format.Format_RGBA8888)
    image.fill(Qt.GlobalColor.transparent)

    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    try:
        if source.kind == "text":
            _draw_text(painter, values, width, height)
        elif source.kind == "shape":
            _draw_shape(painter, values, width, height)
    finally:
        painter.end()

    return _to_array(image)


#: 画面より大きい絵を作るときの一辺の上限（画素） GPU のテクスチャの上限より十分小さく
MAX_CANVAS = 8192


def source_canvas(
    source: GeneratedSource, width: int, height: int, *, frame: int = 0
) -> tuple[int, int]:
    """生成オブジェクトを描く絵の大きさ 画面より大きい図形なら、はみ出す分まで広げる

    中心は画面の中心のまま広げる（描く位置の計算は変えない） 画面の大きさで
    切ってしまうと、画面より大きい図形を回したり動かしたりしたときに、切れた端が
    見えてしまう（YMM4 の斜めの帯のトランジションは高さ 2160 の図形を 45 度回す）
    """
    if source.kind != "shape":
        return width, height
    definition = source_registry.get(source.kind)
    if definition is None:
        return width, height
    values = _resolve(definition, source.params, frame)
    if values.get("shape") == "background":
        return width, height
    if values.get("shape") == "polyline":
        # 線の図形は点の広がりで見積もる
        points = polyline_points(str(values.get("points", "")))
        line = float(values.get("line_width", 0.0))  # type: ignore[arg-type]
        reach_x = max((abs(x) for x, _ in points), default=0.0) + line
        reach_y = max((abs(y) for _, y in points), default=0.0) + line
        values = {**values, "width": reach_x * 2.0, "height": reach_y * 2.0, "line_width": 0.0}
    shape_width = max(1.0, float(values.get("width", 400)))  # type: ignore[arg-type]
    shape_height = max(1.0, float(values.get("height", 400)))  # type: ignore[arg-type]
    line = float(values.get("line_width", 0.0))  # type: ignore[arg-type]
    # 回しても収まるよう、対角線の長さで見積もる
    reach = (shape_width**2 + shape_height**2) ** 0.5 / 2.0 + line
    needed_width = 2.0 * (abs(float(values.get("pos_x", 0.0))) + reach)  # type: ignore[arg-type]
    needed_height = 2.0 * (abs(float(values.get("pos_y", 0.0))) + reach)  # type: ignore[arg-type]
    grown_width = min(MAX_CANVAS, max(width, int(np.ceil(needed_width))))
    grown_height = min(MAX_CANVAS, max(height, int(np.ceil(needed_height))))
    # 画面と偶奇をそろえる 差が奇数だと、中心が半画素ずれて輪郭がにじむ
    grown_width += (grown_width - width) % 2
    grown_height += (grown_height - height) % 2
    return grown_width, grown_height


def _resolve(
    definition: SourceDefinition, params: dict[str, ParamValue], frame: int
) -> dict[str, object]:
    """パラメータを、その時刻での素の値へ

    数値は :class:`~kumiki.core.model.AnimatedValue` なので、フレームを与えて
    評価する ここを飛ばすとキーフレームが効かない
    """
    resolved: dict[str, object] = {}
    for spec in definition.parameters:
        value = spec.coerce(params.get(spec.name))
        resolved[spec.name] = value.at(frame) if isinstance(value, AnimatedValue) else value
    return resolved


def timer_text(values: dict[str, object]) -> str:
    """タイマーの文字 数え下げは、クリップの終わりで初めの値になるように数える

    YMM4 に 2 秒のクリップ・初めの値 0.99 で描かせると、頭で 2、1 秒で 1 だった
    （終わりから逆算した残り時間に初めの値を足している）
    """
    seconds = float(values.get("_seconds", 0.0))  # type: ignore[arg-type]
    rate = float(values.get("timer_rate", 100.0)) / 100.0  # type: ignore[arg-type]
    start = float(values.get("timer_start", 0.0))  # type: ignore[arg-type]
    if bool(values.get("timer_countdown", False)):
        fps = float(values.get("_fps", 30.0))  # type: ignore[arg-type]
        total = float(values.get("timer_length", 0)) / max(fps, 1e-6)  # type: ignore[arg-type]
        value = start + (total - seconds) * rate
    else:
        value = start + seconds * rate
    return format_time(max(value, 0.0), str(values.get("timer_format", "")))


#: 時間の書式で 1 つの文字を並べられる数の上限 壊れたファイルの巨大な書式で固まらないため
MAX_TIME_DIGITS = 9


def format_time(value: float, pattern: str) -> str:
    """.NET の時間の書式（``h`` ``m`` ``s`` ``f`` と ``\\`` の逃がし）で秒を文字にする"""
    value = min(max(value, 0.0), 10.0**9)
    whole = int(value)
    parts = {
        "h": whole // 3600,
        "m": (whole // 60) % 60,
        "s": whole % 60,
    }
    out: list[str] = []
    index = 0
    while index < len(pattern):
        letter = pattern[index]
        if letter == "\\" and index + 1 < len(pattern):
            out.append(pattern[index + 1])
            index += 2
            continue
        run = 1
        while index + run < len(pattern) and pattern[index + run] == letter:
            run += 1
        digits = min(run, MAX_TIME_DIGITS)
        if letter in parts:
            out.append(str(parts[letter]).zfill(digits))
        elif letter in ("f", "F"):
            fraction = value - whole
            out.append(str(int(fraction * 10**digits)).zfill(digits))
        else:
            out.append(letter * digits)
        index += run
    return "".join(out)


def _draw_text(painter: QPainter, values: dict[str, object], width: int, height: int) -> None:
    raw = str(values.get("text", ""))
    if str(values.get("timer_format", "")):
        raw = timer_text(values)[:200]
    text = _revealed(raw, values)
    if not text:
        return

    font = QFont(str(values.get("font", "Yu Gothic UI")))
    font.setPixelSize(max(1, int(float(values.get("size", 64)))))  # type: ignore[arg-type]
    font.setBold(bool(values.get("bold", False)))
    font.setItalic(bool(values.get("italic", False)))
    letter_spacing = float(values.get("letter_spacing", 0.0))  # type: ignore[arg-type]
    if letter_spacing:
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, letter_spacing)

    metrics = QFontMetricsF(font)
    if bool(values.get("vertical", False)):
        _draw_vertical_text(painter, text, font, metrics, values, width, height)
        return

    lines = text.split(chr(10))
    line_height = metrics.height() + float(values.get("line_spacing", 0.0))  # type: ignore[arg-type]
    block_height = line_height * len(lines)

    align = str(values.get("align", "center"))
    centre_x = width / 2.0 + float(values.get("pos_x", 0.0))  # type: ignore[arg-type]
    centre_y = height / 2.0 - float(values.get("pos_y", 0.0))  # type: ignore[arg-type]
    top = centre_y - block_height * _VERTICAL_SHARE.get(str(values.get("valign", "middle")), 0.5)

    # 文字を輪郭（パス）として組み立てる 縁取りを外側だけに出すには、
    # 塗りとは別に輪郭を太らせる必要があり、それはパスでしかできない
    path = QPainterPath()
    for index, line in enumerate(lines):
        line_width = metrics.horizontalAdvance(line)
        if align == "left":
            x = centre_x - _widest(metrics, lines) / 2.0
        elif align == "right":
            x = centre_x + _widest(metrics, lines) / 2.0 - line_width
        else:
            x = centre_x - line_width / 2.0
        baseline = top + line_height * index + metrics.ascent()
        path.addText(QPointF(x, baseline), font, line)

    _paint_glyphs(painter, path, values, width, height)


def _revealed(text: str, values: dict[str, object]) -> str:
    """文字送り 先頭から指定の割合だけを出す

    テロップを 1 文字ずつ出す表現は AviUtl でも定番で、こちらでもキーフレームを
    打てば同じことができる 改行は文字数に数えない 数えると、行が変わる瞬間に
    見た目の速度が変わる
    """
    ratio = float(values.get("reveal", 100.0)) / 100.0  # type: ignore[arg-type]
    if ratio >= 1.0:
        return text
    if ratio <= 0.0:
        return ""

    visible = round(len([c for c in text if c != chr(10)]) * ratio)
    shown: list[str] = []
    for character in text:
        if character == chr(10):
            shown.append(character)
            continue
        if visible <= 0:
            break
        shown.append(character)
        visible -= 1
    return "".join(shown)


def _draw_vertical_text(
    painter: QPainter,
    text: str,
    font: QFont,
    metrics: QFontMetricsF,
    values: dict[str, object],
    width: int,
    height: int,
) -> None:
    """縦書き 行は右から左へ並べる

    Qt に縦書きの組版は無いので、1 文字ずつ縦に置く 日本語のテロップでは
    使う場面がはっきりあるので、簡素でも入れておく
    """
    columns = text.split(chr(10))
    advance = metrics.height() + float(values.get("letter_spacing", 0.0))  # type: ignore[arg-type]
    column_width = metrics.height() + float(values.get("line_spacing", 0.0))  # type: ignore[arg-type]

    centre_x = width / 2.0 + float(values.get("pos_x", 0.0))  # type: ignore[arg-type]
    centre_y = height / 2.0 - float(values.get("pos_y", 0.0))  # type: ignore[arg-type]
    tallest = max((len(column) for column in columns), default=0)
    left = centre_x + column_width * (len(columns) - 1) / 2.0
    top = centre_y - advance * tallest / 2.0

    path = QPainterPath()
    for column_index, column in enumerate(columns):
        x = left - column_width * column_index
        for row_index, character in enumerate(column):
            baseline = top + advance * row_index + metrics.ascent()
            offset = metrics.horizontalAdvance(character) / 2.0
            path.addText(QPointF(x - offset, baseline), font, character)

    _paint_glyphs(painter, path, values, width, height)


def _paint_glyphs(
    painter: QPainter,
    path: QPainterPath,
    values: dict[str, object],
    width: int,
    height: int,
) -> None:
    """組み上がった文字の輪郭を、影・縁取り・塗りの順に描く

    縦書きでも横書きでも飾りの付け方は同じなので、ここに 1 つだけ置く
    順番は下から影・縁・塗り 入れ替えると縁が影を隠す
    """
    shadow = _shadow_layer(path, values, width, height)
    if shadow is not None:
        painter.drawImage(0, 0, shadow)

    border_width = float(values.get("border_width", 0.0))  # type: ignore[arg-type]
    if border_width > 0:
        painter.fillPath(_stroke(path, border_width), _color(values.get("border_color")))
    painter.fillPath(path, _color(values.get("color")))


def _stroke(path: QPainterPath, width: float) -> QPainterPath:
    """輪郭を太らせたパス

    太さは輪郭の中心から両側へ広がるので、指定の 2 倍にして外側に指定幅を出す
    """
    stroker = QPainterPathStroker()
    stroker.setWidth(width * 2.0)
    stroker.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return stroker.createStroke(path)


def _shadow_layer(
    path: QPainterPath, values: dict[str, object], width: int, height: int
) -> QImage | None:
    """文字の影を別の面に描いて返す 影が無ければ ``None``

    ぼかしのために 1 枚離す 影は単色なので、ぼかすのは不透明度だけでよく、
    色の 3 成分はそのままにできる RGB ごとぼかすと、縁で色がにじむ
    """
    offset_x = float(values.get("shadow_x", 0.0))  # type: ignore[arg-type]
    offset_y = float(values.get("shadow_y", 0.0))  # type: ignore[arg-type]
    blur = float(values.get("shadow_blur", 0.0))  # type: ignore[arg-type]
    colour = _color(values.get("shadow_color"))
    if (offset_x, offset_y, blur) == (0.0, 0.0, 0.0) or colour.alpha() == 0:
        return None

    layer = QImage(width, height, QImage.Format.Format_RGBA8888)
    layer.fill(Qt.GlobalColor.transparent)
    shifted = QPainterPath(path)
    # 画面の Y は下向き 設定の Y は上向きなので符号を反転する
    shifted.translate(offset_x, -offset_y)

    shadow_painter = QPainter(layer)
    shadow_painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    border_width = float(values.get("border_width", 0.0))  # type: ignore[arg-type]
    if border_width > 0:
        # 縁取りがあるときは、その外形の影が落ちる 塗りだけの影にすると
        # 縁の分だけ影が細く見える
        shadow_painter.fillPath(_stroke(shifted, border_width), colour)
    shadow_painter.fillPath(shifted, colour)
    shadow_painter.end()

    if blur <= 0.0:
        return layer
    return _blur_alpha(layer, blur)


def _blur_alpha(image: QImage, radius: float) -> QImage:
    """不透明度だけを平均化する

    箱ぼかしを縦横 2 回ずつ掛ける 厳密なガウスではないが、影の輪郭を
    やわらげる用途では見分けが付かず、こちらは掛け算が要らない
    """
    # 窓の幅は絵の大きさまで 壊れたファイルの巨大な値でも、絵より広くぼかす意味は無い
    # （そのまま渡すと、埋めた配列が何百 GB にもなって落ちる）
    span = max(1, min(round(radius), max(image.width(), image.height())))
    # ``_to_array`` は元のバッファをそのまま見ていることがあり、書き込めない
    array = _to_array(image).copy()
    alpha = array[:, :, 3].astype(np.float32)
    for _ in range(2):
        alpha = _box_blur(alpha, span)
    array[:, :, 3] = np.clip(alpha, 0.0, 255.0).astype(np.uint8)
    return QImage(
        array.tobytes(), image.width(), image.height(), QImage.Format.Format_RGBA8888
    ).copy()


def _box_blur(values: np.ndarray, span: int) -> np.ndarray:
    """縦横に窓幅 ``2*span+1`` の移動平均を掛ける

    累積和で求めるので、窓の幅を広げても速さは変わらない
    """
    result = values
    for axis in (0, 1):
        padded = np.pad(result, [(span, span) if a == axis else (0, 0) for a in (0, 1)], "edge")
        cumulative = np.cumsum(padded, axis=axis)
        zero = np.zeros_like(np.take(cumulative, [0], axis=axis))
        cumulative = np.concatenate([zero, cumulative], axis=axis)
        length = result.shape[axis]
        upper = np.take(cumulative, range(2 * span + 1, 2 * span + 1 + length), axis=axis)
        lower = np.take(cumulative, range(0, length), axis=axis)
        result = (upper - lower) / (2 * span + 1)
    return result


def _widest(metrics: QFontMetricsF, lines: list[str]) -> float:
    return max((metrics.horizontalAdvance(line) for line in lines), default=0.0)


def _draw_shape(painter: QPainter, values: dict[str, object], width: int, height: int) -> None:
    shape_width = max(1.0, float(values.get("width", 400)))  # type: ignore[arg-type]
    shape_height = max(1.0, float(values.get("height", 400)))  # type: ignore[arg-type]
    centre_x = width / 2.0 + float(values.get("pos_x", 0.0))  # type: ignore[arg-type]
    centre_y = height / 2.0 - float(values.get("pos_y", 0.0))  # type: ignore[arg-type]

    if str(values.get("shape", "rect")) == "background":
        # 背景は大きさの設定を見ず、絵の全体を塗る 設定の大きさで描くと、画面より
        # 大きく広げた絵や、大きさを持たない読み込み元（YMM4 の背景）で隙間が出る
        shape_width = float(width) + abs(float(values.get("pos_x", 0.0))) * 2.0  # type: ignore[arg-type]
        shape_height = float(height) + abs(float(values.get("pos_y", 0.0))) * 2.0  # type: ignore[arg-type]
    if str(values.get("shape", "rect")) == "polyline":
        _draw_polyline(painter, values, centre_x, centre_y)
        return
    if str(values.get("shape", "rect")) == "concentration":
        _draw_concentration(painter, values, centre_x, centre_y, width, height)
        return
    rect = QRectF(-shape_width / 2.0, -shape_height / 2.0, shape_width, shape_height)
    path = _shape_path(str(values.get("shape", "rect")), rect, values)

    transform = QTransform()
    transform.translate(centre_x, centre_y)
    transform.rotate(float(values.get("rotation", 0.0)))  # type: ignore[arg-type]
    path = transform.map(path)

    color = _color(values.get("color"))
    line_width = float(values.get("line_width", 0.0))  # type: ignore[arg-type]

    if bool(values.get("outline_only", False)):
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(color, max(line_width, 1.0)))
        painter.drawPath(path)
        return

    painter.fillPath(path, color)
    if line_width > 0:
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(color, line_width))
        painter.drawPath(path)


def polyline_points(text: str) -> list[tuple[float, float]]:
    """``"x,y;x,y"`` を点の並びへ 読めない組は飛ばす 座標は中心からの画素で Y は上が正

    点の数は :data:`MAX_POLYLINE_POINTS` で頭を抑える 壊れたファイルや巨大な線を
    読み込むと、1 フレームごとに点列を解き直して再生が止まる
    """
    points: list[tuple[float, float]] = []
    for pair in text.split(";", MAX_POLYLINE_POINTS):
        if len(points) >= MAX_POLYLINE_POINTS:
            break
        parts = pair.split(",")
        if len(parts) != 2:
            continue
        try:
            x, y = float(parts[0]), float(parts[1])
        except ValueError:
            continue
        if np.isfinite(x) and np.isfinite(y):
            points.append((x, y))
    return points


def _polyline_path(values: dict[str, object], centre_x: float, centre_y: float) -> QPainterPath:
    points = [
        # 点も Y は上が正 ほかの位置の設定と向きがそろう
        (centre_x + x, centre_y - y)
        for x, y in polyline_points(str(values.get("points", "")))
    ]
    path = QPainterPath()
    if len(points) < 2:
        return path
    closed = bool(values.get("closed", False))
    path.moveTo(*points[0])
    if str(values.get("line_type", "straight")) == "quadratic" and len(points) >= 3:
        # 2 次ベジェ 点を 1 つおきに制御点として読む（YMM4 の QuadraticBezier）
        # 閉じるときは始点へ戻る曲線にする
        sequence = [*points, points[0]] if closed else points
        index = 1
        while index + 1 < len(sequence):
            control, end = sequence[index], sequence[index + 1]
            path.quadTo(control[0], control[1], end[0], end[1])
            index += 2
        if index < len(sequence):
            path.lineTo(*sequence[index])
    else:
        for point in points[1:]:
            path.lineTo(*point)
    if closed:
        path.closeSubpath()
    return path


def _draw_concentration(
    painter: QPainter,
    values: dict[str, object],
    centre_x: float,
    centre_y: float,
    width: int,
    height: int,
) -> None:
    """集中線（YMM4 の ConcentrationLine） 中心から放つ細い三角を、半径で濃さを変えて描く

    本数・太さ・長さ・ぼかしの効き方は YMM4 に描かせた絵から近づけた ぼかし 0 は大きさの
    半分の円の中に硬い線、ぼかすと線は画面の外まで伸びて、中心側がぼんやり抜ける
    """
    radius = max(float(values.get("width", 400)) * 0.5, 1.0)  # type: ignore[arg-type]
    count = max(1, min(1000, int(float(values.get("density", 80)))))  # type: ignore[arg-type]
    thickness = float(values.get("line_thickness", 50.0)) / 100.0  # type: ignore[arg-type]
    length = float(values.get("line_length", 70.0)) / 100.0  # type: ignore[arg-type]
    soft = max(0.0, min(1.0, float(values.get("softness", 50.0)) / 100.0))  # type: ignore[arg-type]
    flicker = float(values.get("flicker", 5.0))  # type: ignore[arg-type]
    seconds = float(values.get("_seconds", 0.0))  # type: ignore[arg-type]
    tick = int(seconds * flicker) if flicker > 0 else 0
    random = np.random.default_rng(tick * 7919 + 17)

    inner = radius * (1.0 - length)
    far = radius * (1.0 + soft * 2.0) if soft > 0 else radius
    reach = max(far, float(np.hypot(width, height)))
    gradient = QRadialGradient(centre_x, centre_y, far)
    colour = _color(values.get("color"))
    # ぼかすと線は半透明になる（YMM4 の絵は白い線でも 170 ほどで、真っ白にならない）
    colour.setAlphaF(colour.alphaF() * (1.0 - soft * 0.45))
    clear = QColor(colour)
    clear.setAlpha(0)
    if soft <= 0:
        gradient.setColorAt(0.0, colour)
        gradient.setColorAt(1.0, colour)
    else:
        start = max(0.0, (inner - radius * soft) / far)
        full = min(1.0, (inner + radius * soft) / far)
        gradient.setColorAt(0.0, clear)
        gradient.setColorAt(start, clear)
        gradient.setColorAt(max(full, start + 1e-3), colour)
        gradient.setColorAt(min(1.0, radius / far), colour)
        gradient.setColorAt(1.0, clear)
    path = QPainterPath()
    spacing = 2.0 * np.pi / count
    for _ in range(count):
        angle = random.uniform(0.0, 2.0 * np.pi)
        half = spacing * thickness * random.uniform(0.2, 1.0) * 0.5
        start_radius = inner * random.uniform(0.8, 1.2) if soft <= 0 else 0.0
        end_radius = radius if soft <= 0 else reach
        tip_x = centre_x + np.cos(angle) * start_radius
        tip_y = centre_y + np.sin(angle) * start_radius
        path.moveTo(tip_x, tip_y)
        path.lineTo(
            centre_x + np.cos(angle - half) * end_radius,
            centre_y + np.sin(angle - half) * end_radius,
        )
        path.lineTo(
            centre_x + np.cos(angle + half) * end_radius,
            centre_y + np.sin(angle + half) * end_radius,
        )
        path.closeSubpath()
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(gradient))
    painter.drawPath(path)


#: 折れ線で読み取る点の数の上限 これ以上は捨てる（描画は 1 フレームごとに走る）
MAX_POLYLINE_POINTS = 4096

#: 線の一部を描き直すときの刻みの上限 長い線で刻みが増えると、1 フレームに何秒もかかる
MAX_TRIM_STEPS = 2000


def _trimmed(path: QPainterPath, start: float, end: float) -> QPainterPath:
    """線の途中だけを残す ``start`` と ``end`` は全長に対する 0..1"""
    if start <= 0.0 and end >= 1.0:
        return path
    if end <= start:
        return QPainterPath()
    total = path.length()
    trimmed = QPainterPath()
    # 2 画素ごとに点を打つ 長い線でも刻みが増えすぎないように頭を抑える
    steps = max(8, min(int(total / 2.0), MAX_TRIM_STEPS))
    first = True
    for index in range(steps + 1):
        fraction = start + (end - start) * index / steps
        point = path.pointAtPercent(path.percentAtLength(total * fraction))
        if first:
            trimmed.moveTo(point)
            first = False
        else:
            trimmed.lineTo(point)
    return trimmed


def _draw_polyline(
    painter: QPainter, values: dict[str, object], centre_x: float, centre_y: float
) -> None:
    """線の図形 閉じていれば中を塗ってから線を引く 端と角は丸める（配布物はすべて丸）"""
    path = _polyline_path(values, centre_x, centre_y)
    if path.isEmpty():
        return
    trim_start = float(values.get("trim_start", 0.0)) / 100.0  # type: ignore[arg-type]
    trim_end = float(values.get("trim_end", 100.0)) / 100.0  # type: ignore[arg-type]
    path = _trimmed(path, max(0.0, trim_start), min(1.0, trim_end))
    if path.isEmpty():
        return
    if bool(values.get("closed", False)):
        fill = _color(values.get("fill_color"))
        if fill.alpha() > 0:
            painter.fillPath(path, fill)
    width = float(values.get("line_width", 0.0))  # type: ignore[arg-type]
    if width <= 0:
        return
    pen = QPen(_color(values.get("color")), width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    dashes = [float(v) for v in str(values.get("dash", "")).split(",") if _is_number(v)]
    if len(dashes) >= 2 and all(v >= 0 for v in dashes) and sum(dashes) > 0:
        # 破線の長さは線の太さを 1 とする割合（Qt も YMM4 も同じ決まり）
        pen.setDashPattern(dashes)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(pen)
    painter.drawPath(path)


def _is_number(text: str) -> bool:
    try:
        return bool(np.isfinite(float(text)))
    except ValueError:
        return False


def _shape_path(kind: str, rect: QRectF, values: dict[str, object]) -> QPainterPath:
    path = QPainterPath()
    if kind == "ellipse":
        path.addEllipse(rect)
    elif kind == "rounded":
        radius = float(values.get("corner_radius", 24))  # type: ignore[arg-type]
        path.addRoundedRect(rect, radius, radius)
    elif kind == "triangle":
        path.moveTo(rect.center().x(), rect.top())
        path.lineTo(rect.right(), rect.bottom())
        path.lineTo(rect.left(), rect.bottom())
        path.closeSubpath()
    elif kind == "pentagon":
        path = _polygon_path(rect, 5)
    elif kind == "hexagon":
        path = _polygon_path(rect, 6)
    elif kind == "star":
        path = _star_path(rect)
    elif kind == "inscribed_triangle":
        # 頂点を上にして楕円に内接する三角形（YMM4 の三角形） 四角に合わせた三角形とは
        # 底辺の位置と幅が違う
        path = _polygon_path(rect, 3)
    elif kind == "fan":
        path = _fan_path(rect, float(values.get("span", 360.0)))  # type: ignore[arg-type]
    elif kind == "arrow":
        path = _arrow_path(
            rect,
            float(values.get("bar_length", 50.0)),  # type: ignore[arg-type]
            float(values.get("bar_thickness", 50.0)),  # type: ignore[arg-type]
        )
    elif kind == "superformula":
        path = _superformula_path(
            rect,
            float(values.get("formula_m", 4.0)),  # type: ignore[arg-type]
            float(values.get("formula_n", 1.0)),  # type: ignore[arg-type]
        )
    else:
        # ``background`` もここ 大きさは呼び出し側が画面いっぱいに指定する
        path.addRect(rect)
    return path


def _polygon_path(rect: QRectF, sides: int) -> QPainterPath:
    """正多角形 頂点を上に向けて置く

    AviUtl の五角形・六角形に対応する 頂点の向きを合わせておかないと、
    移植した資産で図形だけ傾いて見える
    """
    path = QPainterPath()
    radius_x, radius_y = rect.width() / 2.0, rect.height() / 2.0
    centre = rect.center()
    for index in range(sides):
        angle = 2.0 * np.pi * index / sides - np.pi / 2.0
        x = centre.x() + np.cos(angle) * radius_x
        y = centre.y() + np.sin(angle) * radius_y
        if index == 0:
            path.moveTo(x, y)
        else:
            path.lineTo(x, y)
    path.closeSubpath()
    return path


def _star_path(rect: QRectF, points: int = 5) -> QPainterPath:
    """5 芒星 外周と内周の頂点を交互に結ぶ"""
    path = QPainterPath()
    outer_x, outer_y = rect.width() / 2.0, rect.height() / 2.0
    inner_ratio = 0.382  # 正五芒星の内接比
    centre = rect.center()

    for index in range(points * 2):
        angle = np.pi * index / points - np.pi / 2.0
        ratio = 1.0 if index % 2 == 0 else inner_ratio
        x = centre.x() + np.cos(angle) * outer_x * ratio
        y = centre.y() + np.sin(angle) * outer_y * ratio
        if index == 0:
            path.moveTo(x, y)
        else:
            path.lineTo(x, y)
    path.closeSubpath()
    return path


def _fan_path(rect: QRectF, span: float) -> QPainterPath:
    """扇 上を 0 として反時計回りに ``span`` 度（YMM4 の CenterAngle）"""
    path = QPainterPath()
    path.moveTo(rect.center())
    # Qt の角度は右が 0 で反時計回り 上（90 度）から反時計回りに広げる
    path.arcTo(rect, 90.0, max(0.0, min(span, 360.0)))
    path.closeSubpath()
    return path


def _arrow_path(rect: QRectF, bar_length: float, bar_thickness: float) -> QPainterPath:
    """上向きの矢印 頭は楕円に内接する三角形、軸は頭の底辺から下へ伸びる

    軸の長さは半径の 3 倍を 100、太さは半径を 100 とする割合（YMM4 の絵に合わせた）
    """
    radius_x, radius_y = rect.width() / 2.0, rect.height() / 2.0
    centre = rect.center()
    base_y = centre.y() + radius_y * 0.5
    half_head = radius_x * np.sqrt(3.0) / 2.0
    half_bar = radius_x * max(bar_thickness, 0.0) / 200.0
    bar_end = base_y + radius_y * 3.0 * max(bar_length, 0.0) / 100.0
    path = QPainterPath()
    path.moveTo(centre.x(), centre.y() - radius_y)
    path.lineTo(centre.x() + half_head, base_y)
    path.lineTo(centre.x() + half_bar, base_y)
    path.lineTo(centre.x() + half_bar, bar_end)
    path.lineTo(centre.x() - half_bar, bar_end)
    path.lineTo(centre.x() - half_bar, base_y)
    path.lineTo(centre.x() - half_head, base_y)
    path.closeSubpath()
    return path


def _superformula_path(rect: QRectF, m: float, n: float) -> QPainterPath:
    """スーパーフォーミュラ（Gielis の式、n1 = n2 = n3 = n） 一番遠い点が楕円に届くよう縮める"""
    angles = np.linspace(0.0, 2.0 * np.pi, 721)
    exponent = max(abs(n), 0.05)
    quarter = m * angles / 4.0
    radius = (np.abs(np.cos(quarter)) ** exponent + np.abs(np.sin(quarter)) ** exponent) ** (
        -1.0 / exponent
    )
    radius = np.nan_to_num(radius, nan=0.0, posinf=0.0)
    peak = float(radius.max()) if radius.size else 1.0
    radius = radius / (peak if peak > 0 else 1.0)
    centre = rect.center()
    path = QPainterPath()
    for index, (angle, r) in enumerate(zip(angles, radius, strict=True)):
        x = centre.x() + np.cos(angle - np.pi / 2.0) * r * rect.width() / 2.0
        y = centre.y() + np.sin(angle - np.pi / 2.0) * r * rect.height() / 2.0
        if index == 0:
            path.moveTo(x, y)
        else:
            path.lineTo(x, y)
    path.closeSubpath()
    return path


def _color(value: object) -> QColor:
    """``(R, G, B, A)`` の 0..1（sRGB）を :class:`QColor` へ"""
    if not isinstance(value, tuple) or len(value) < 3:
        return QColor(255, 255, 255, 255)
    channels = [round(min(max(float(v), 0.0), 1.0) * 255) for v in value[:4]]
    while len(channels) < 4:
        channels.append(255)
    return QColor(*channels)


def _to_array(image: QImage) -> np.ndarray:
    """``QImage`` を ``(高さ, 幅, 4)`` の配列へ

    ``QImage`` は行ごとに詰め物を入れることがあるので、``bytesPerLine`` を見て
    余りを落とす 幅だけで整形すると絵が斜めにずれる
    """
    width, height = image.width(), image.height()
    buffer = bytes(image.constBits())
    stride = image.bytesPerLine()
    rows = np.frombuffer(buffer, dtype=np.uint8).reshape(height, stride)
    return np.ascontiguousarray(rows[:, : width * 4].reshape(height, width, 4))
